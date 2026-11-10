"""짧은 멈춤 말("그만", "멈춰", "stop" 등)을 말하는 동안의 마이크 소리에서 찾는다.

이어폰 없이 쓰면 스피커에서 나온 캐릭터 목소리도 마이크에 들어온다. 그래서
- 말하는 동안의 마이크 프레임을 짧은 조각으로 나눠 따로 받아 적고(평소 듣기 흐름에는 손대지 않는다),
- 받아 적은 글에 멈춤 말이 있고, 그 말이 방금 캐릭터가 한 말에 들어 있지 않을 때만(자기 목소리 거르기),
- 받아 적은 글이 멈춤 말보다 지나치게 길지 않을 때만(섞인 소리·긴 문장 거르기) 멈춘다.
원본 AI_Partner listen/barge_in.py 에서 대화모드 부분(의도 분류·발화 인계)은 뺐다(대화모드 걸음에서 되살린다).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from difflib import SequenceMatcher

import numpy as np

from voicelib.listen import FRAME_SAMPLES, SAMPLE_RATE

FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
SPEECH_PROBABILITY = 0.5
END_SILENCE = 0.4  # 말소리 뒤 이만큼 조용하면 한 조각이 끝난 것으로 본다(초)
MIN_SPEECH = 0.25  # 이보다 짧은 소리는 받아 적지 않는다(초)
MAX_SEGMENT = 4.0  # 한 조각의 최대 길이(초). 넘으면 그때까지를 받아 적는다
PRE_ROLL = 0.2  # 말소리가 시작되기 조금 앞부터 담는다(초)
EXTRA_CHARS = 10  # 받아 적은 글이 멈춤 말보다 이보다 많이 길면 멈춤 명령으로 보지 않는다(글자, 공백·기호 제외)


def normalize(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def is_output_echo(heard: str, spoken: str) -> bool:
    """받아 적은 글이 방금 캐릭터가 한 말(스피커 소리)인지. 짧은 말은 정확히 일치할 때만 거른다."""
    text, output = normalize(heard), normalize(spoken)
    if text and output and (text == output or (len(text) >= 4 and text in output)):
        return True
    if len(text) < 8 or not output:
        return False
    if text in output:
        return True
    # 받아쓰기가 낱말 몇 개를 바꿔 적은 자기 음성도 거른다. 3글자 이상 이어서 같은 부분만 센다
    blocks = SequenceMatcher(None, text, output, autojunk=False).get_matching_blocks()
    matched = sum(b.size for b in blocks if b.size >= 3)
    return matched / len(text) >= 0.55 and len(text) - matched <= 5


def match_stop(heard: str, spoken: str, phrases, pause_phrases=(), extra_chars: int = EXTRA_CHARS) -> str | None:
    """받아 적은 글이 멈춤 명령이면 그 멈춤 말, 아니면 None. pause_phrases 는 말 전체가 그것일 때만 인정한다."""
    said = normalize(heard)
    if not said:
        return None
    echo = normalize(spoken)
    if echo and said in echo:
        return None  # 캐릭터 자신의 목소리가 들어왔다
    pause = {normalize(p) for p in pause_phrases}
    for phrase in sorted(dict.fromkeys((*phrases, *pause_phrases)), key=len, reverse=True):
        key = normalize(phrase)
        if key in pause and said != key:
            continue
        if key and key in said and key not in echo and len(said) <= len(key) + extra_chars:
            return phrase
    return None


def is_pause_phrase(text: str, pause_phrases) -> bool:
    """일반 문장으로도 쓰이는 짧은 끼어들기 표현인지."""
    return normalize(text) in {normalize(p) for p in pause_phrases}


class BargeIn:
    """말하는 동안의 마이크 프레임을 받아 짧은 조각으로 나누고, 뒤 스레드에서 받아 적어 멈춤 말을 찾는다.

    transcribe(소리) → 글, spoken() → 방금 캐릭터가 한 말, on_stop(멈춤 말, 받아 적은 글) 은 멈춤 말을 찾았을 때 불린다.
    받아 적는 중에 새 조각이 오면 가장 최근 조각 하나만 남긴다(밀리면 늦은 멈춤은 쓸모가 없다).
    """

    def __init__(self, phrases, transcribe: Callable[[np.ndarray], str], spoken: Callable[[], str],
                 on_stop: Callable[[str, str], None], on_error: Callable[[str], None] | None = None,
                 pause_phrases=()) -> None:
        self.phrases = tuple(phrases)
        self.pause_phrases = tuple(pause_phrases)
        self._transcribe = transcribe
        self._spoken = spoken
        self._on_stop = on_stop
        self._on_error = on_error
        self._pre: list[np.ndarray] = []
        self._frames: list[np.ndarray] = []
        self._speech = 0
        self._silence = 0
        self._pending: np.ndarray | None = None
        self._pending_spoken = None
        self._frames_spoken = None
        self._generation = 0  # reset 할 때마다 오른다. 지난 말하기의 조각 결과로 다음 말을 멈추지 않게 한다
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._work, daemon=True, name="barge_in")
        self._thread.start()

    # 듣는 스레드에서 부른다

    def feed(self, frame: np.ndarray, probability: float) -> None:
        speech = probability >= SPEECH_PROBABILITY
        if not self._frames:
            if not speech:
                self._pre.append(frame)
                del self._pre[: max(0, len(self._pre) - round(PRE_ROLL / FRAME_SECONDS))]
                return
            self._frames = list(self._pre)
            self._frames_spoken = self._spoken()
            self._pre = []
            self._speech = self._silence = 0
        self._frames.append(frame)
        spoken = self._spoken()
        if spoken and spoken not in (self._frames_spoken or ""):
            self._frames_spoken = ((self._frames_spoken or "") + " " + spoken).strip()
        if speech:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence * FRAME_SECONDS >= END_SILENCE or len(self._frames) * FRAME_SECONDS >= MAX_SEGMENT:
            self._finish()

    def reset(self) -> None:
        """말하기가 끝났다. 모으던 조각과 기다리던 받아 적기를 버린다."""
        self._pre, self._frames, self._speech, self._silence = [], [], 0, 0
        self._frames_spoken = None
        with self._lock:
            self._pending = None
            self._pending_spoken = None
            self._generation += 1

    def close(self) -> None:
        self._closed.set()
        self._wake.set()

    # 안쪽

    def _finish(self) -> None:
        frames, speech = self._frames, self._speech
        spoken, self._frames_spoken = self._frames_spoken, None
        self._frames, self._speech, self._silence = [], 0, 0
        if speech * FRAME_SECONDS < MIN_SPEECH:
            return
        with self._lock:
            self._pending = np.concatenate(frames)
            self._pending_spoken = self._spoken() if spoken is None else spoken
        self._wake.set()

    def _work(self) -> None:
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                audio, self._pending = self._pending, None
                generation = self._generation
                spoken = self._pending_spoken
            if audio is None or self._closed.is_set():
                continue
            try:
                heard = self._transcribe(audio)
            except Exception as e:  # 받아 적기 실패로 듣는 쪽이 멈추지 않게 한다
                if self._on_error is not None and generation == self._generation and not self._closed.is_set():
                    self._on_error(f"{type(e).__name__}: {e}")
                continue
            spoken = self._spoken() if spoken is None else spoken
            if is_output_echo(heard, spoken):
                continue
            phrase = match_stop(heard, spoken, self.phrases, self.pause_phrases)
            with self._lock:
                stale = generation != self._generation
            if phrase is not None and not stale:
                self._on_stop(phrase, heard)
