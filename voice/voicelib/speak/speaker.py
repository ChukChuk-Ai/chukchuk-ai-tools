"""글을 문장 단위로 소리로 만들어 차례로 재생한다.

원본 AI_Partner speak/speaker.py 에서 듣기·말하기에 쓰는 부분만 옮겼다(고정 안내 캐시·응답 받는 대로 말하기·모드별 음성은 뺐다).
"""

from __future__ import annotations

import queue
import re
import threading
from collections import deque
import time
from collections.abc import Callable

import numpy as np

from voicelib.speak.reference import PlaybackReference
from voicelib.speak.tts import TTSEngine, TTSError

# 문장 끝 부호 뒤의 공백에서 나눈다
# 일본어·중국어는 문장 끝(。！？) 뒤에 공백이 없으므로 공백 없이도 끊는다(닫는 따옴표·괄호 앞은 제외). 그래야 첫 문장이 끝나는 대로 말하기 시작한다
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？])(?![」』）)”’])\s*")
# 이보다 짧은 조각은 다음 문장에 붙인다. 너무 잘게 끊으면 말이 어색해진다
MIN_SENTENCE_CHARS = 12
# 첫 문장이 길면 첫 쉼표에서 끊어 앞부분을 먼저 소리로 만든다(2026-10-14 측정: 합성 시간은 글자 수에 따라 늘어
# 약 25자를 넘으면 2.5초 안팎, 쉼표 앞만 만들면 중앙값 약 0.5초 빠름). 쉼표 앞이 이 글자 수 이상일 때만 끊는다
FIRST_CLAUSE_MIN_CHARS = 10
# 첫 문장이 이 글자 수를 넘을 때만 끊는다(미리 다 아는 경우). 짧은 문장은 한 번에 말하는 편이 자연스럽다
FIRST_CLAUSE_LONG_CHARS = 25
# 쉼표 뒤에 공백이 있고 쉼표 앞이 숫자가 아닐 때만 쉼표로 본다("1,000원" 같은 숫자는 끊지 않는다)
_COMMA = re.compile(r"(?<=[^\d\s])(?:,\s+|[，、]\s*)")


def first_clause(text: str) -> re.Match | None:
    """text 의 첫 쉼표(앞이 FIRST_CLAUSE_MIN_CHARS 글자 이상)를 찾는다. 없으면 None."""
    for match in _COMMA.finditer(text):
        if len(text[: match.start() + 1].strip()) >= FIRST_CLAUSE_MIN_CHARS:
            return match
        # 앞이 너무 짧은 쉼표("네, ...")는 건너뛰고 더 뒤의 쉼표를 보지 않는다(첫 쉼표만 본다)
        return None
    return None


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    carry = ""
    for piece in _SENTENCE_END.split(text.strip()):
        carry = f"{carry} {piece}".strip()
        if len(carry) >= MIN_SENTENCE_CHARS:
            sentences.append(carry)
            carry = ""
    if carry:
        if sentences:
            sentences[-1] = f"{sentences[-1]} {carry}"
        else:
            sentences.append(carry)
    # 첫 문장이 길면 첫 쉼표에서 끊어 앞부분부터 말한다(첫 소리가 빨라진다)
    if sentences and len(sentences[0]) > FIRST_CLAUSE_LONG_CHARS and (cut := first_clause(sentences[0])):
        head, rest = sentences[0][: cut.start() + 1], sentences[0][cut.end() :].strip()
        sentences[0:1] = [head, rest] if rest else [head]
    return sentences


def with_lead_in(audio: np.ndarray, rate: int, lead_in: float, level: float = 0.0) -> tuple[np.ndarray, float]:
    """소리가 시작되기 전의 무음이 lead_in 초가 되도록 앞에 무음을 붙인다. (소리, 붙인 길이(초))를 돌려준다.

    스피커나 오디오 장치는 재생을 시작한 직후 잠깐 소리를 내지 못해 첫 음절이 잘린다.
    음성 합성 결과에 원래 있던 앞 무음은 빼고 모자란 만큼만 붙인다.
    """
    loud = np.flatnonzero(np.abs(audio) > level)
    have = loud[0] / rate if len(loud) else 0.0
    need = max(0.0, lead_in - have)
    if need == 0.0:
        return audio, 0.0
    return np.concatenate([np.zeros(round(need * rate), dtype=audio.dtype), audio]), need


START_TONE_HZ = 800.0
START_TONE_SECONDS = 0.08


def startup_cue(rate: int, seconds: float, noise: float = 0.0, tone: float = 0.0) -> np.ndarray:
    """출력 시작 보호 구간. 길이는 seconds(신호 포함)이다.

    완전한 0(디지털 무음)만 보내면 일부 장치는 절전 상태에서 깨어나지 않아 뒤따르는 첫 음절이 잘린다.
    2026-10-26 실기 시험(scripts/trial_lead_in.py): 무음 1.5초·작은 잡음(0.002)·스트림 선개방은 앞이 잘렸고,
    짧고 부드러운 신호(800Hz 0.08초, 크기 0.05) 뒤 무음 간격을 둔 방식이 잘리지 않으면서 거슬리지 않았다.
    tone 은 그 신호의 크기(0이면 신호 없음), noise 는 나머지 구간에 까는 잡음의 크기(0이면 무음)다. 긴 비프음은 쓰지 않는다.
    """
    frames = max(0, round(seconds * rate))
    if not frames:
        return np.zeros(0, dtype='float32')
    cue = np.zeros(frames, dtype='float32')
    if noise > 0:
        rng = np.random.default_rng(0)
        cue = ((rng.random(frames, dtype='float32') * 2 - 1) * noise).astype('float32')
    if tone > 0:
        count = min(round(START_TONE_SECONDS * rate), frames // 2)
        if count:
            wave = np.sin(2 * np.pi * START_TONE_HZ * np.arange(count) / rate) * tone
            fade = min(count // 4, round(rate * 0.02))
            if fade:
                ramp = np.linspace(0, 1, fade)
                wave[:fade] *= ramp
                wave[-fade:] *= ramp[::-1]
            cue[:count] = wave.astype('float32')
    return cue


ECHO_TAIL_SECONDS = 6.0  # 재생 종료 뒤 이 시간 안에 접수된 소리는 자기 음성 비교 대상(STT 지연 포함)
RECENT_SECONDS = 20.0 # 이 시간 안에 말한 문장을 자기 목소리로 본다(초)


class PlaybackError(TTSError):
    """소리를 낼 장치를 열지 못했다. 장치가 바뀌었거나 없을 때 난다."""


def _play(audio: np.ndarray, sample_rate: int, stopped: threading.Event) -> None:
    import sounddevice as sd
    from voicelib.audio_devices import portaudio_use

    try:
        with portaudio_use():
            sd.play(audio, sample_rate)
            try:
                while sd.get_stream().active:
                    if stopped.is_set():
                        break
                    time.sleep(0.03)
            finally:
                sd.stop()
    except sd.PortAudioError as e:
        raise PlaybackError(f"output device: {e}") from e


class ContinuousPlayer:
    """출력 스트림을 공유하고 새로 열 때마다 원음 앞에 보호 무음을 쓴다."""

    def __init__(self, *, lead_in: float = 0.0, lead_in_noise: float = 0.0,
                 lead_in_tone: float = 0.0, on_audio: Callable[[np.ndarray, int], None] | None = None) -> None:
        self._stream = None
        self._rate = None
        self._output_rate = None
        self._converter = None
        self._closed = threading.Event()
        self._stream_control = threading.Lock()
        self._stream_aborted = False
        self.underflows = 0
        self._stopped = None
        self._lead_in = lead_in
        self._lead_in_noise = lead_in_noise
        self._lead_in_tone = lead_in_tone
        self._on_audio = on_audio

    def __call__(self, audio: np.ndarray, rate: int, stopped: threading.Event) -> None:
        import sounddevice as sd
        try:
            if stopped.is_set():
                return
            self._stopped = stopped
            if self._stream is not None and rate != self._rate:
                self.close()
            if self._stream is None:
                self._closed = closed = threading.Event()
                from voicelib.speak.output import open_output, RateConverter
                self._stream, self._output_rate = open_output(sd, rate, self._lead_in)
                self._stream_aborted = False
                self._rate = rate
                self._converter = RateConverter(rate, self._output_rate) if rate != self._output_rate else None
                stream = self._stream
                def watch():
                    while not closed.wait(0.03):
                        if stopped.is_set():
                            # PortAudio 객체가 감시 스레드에서 사용되는 동안 다른 쪽에서
                            # stop/close하지 않는다. 이전 스트림의 감시가 새 장치를 건드리지 않는다.
                            with self._stream_control:
                                if closed.is_set() or self._stream is not stream:
                                    return
                                try:
                                    stream.abort()
                                    self._stream_aborted = True
                                except sd.PortAudioError:
                                    pass
                            return
                threading.Thread(target=watch, daemon=True).start()
                # 비프음 없이 무음으로 출력 시작을 준비한다.
                # 재연결·샘플레이트 변경에도 적용하고 원음은 한 샘플도 자르지 않는다.
                if self._converter is not None:
                    audio = self._converter.convert(audio)
                cue = startup_cue(self._output_rate, self._lead_in, self._lead_in_noise, self._lead_in_tone)
                if len(cue):
                    audio = np.concatenate([cue, audio])
            elif self._converter is not None:
                audio = self._converter.convert(audio)
            self._write(audio, stopped)
        except sd.PortAudioError as error:
            self.close(abort=True)
            if not stopped.is_set():
                raise PlaybackError(f"output device: {error}") from error

    def _write(self, audio, stopped):
        rate = self._output_rate
        if self._on_audio is not None:
            self._on_audio(audio, rate)
        block = max(1, int(rate * 0.05))
        for start in range(0, len(audio), block):
            if stopped.is_set():
                return
            chunk = np.ascontiguousarray(audio[start:start + block], dtype="float32")
            if self._stream.write(chunk):
                self.underflows += 1

    def close(self, *, abort: bool = False) -> None:
        if self._stream is None:
            return
        stream = self._stream
        try:
            if not abort and self._converter is not None:
                tail = self._converter.convert()
                if len(tail):
                    self._write(tail, self._stopped or threading.Event())
        finally:
            # 감시의 abort 완료를 기다린 뒤 닫는다. 중복 close도 같은 장치를 다시 쓰지 않는다.
            with self._stream_control:
                if self._stream is stream:
                    try:
                        self.underflows += getattr(stream, "underflows", 0)
                        cancelled = abort or (self._stopped is not None and self._stopped.is_set())
                        if not self._stream_aborted:
                            if cancelled:
                                stream.abort()
                            else:
                                stream.stop()  # 정상 종료는 마지막 장치 버퍼까지 재생한다.
                    except Exception as error:
                        import sounddevice as sd
                        if not isinstance(error, sd.PortAudioError):
                            raise
                        if self._stopped is None or not self._stopped.is_set():
                            raise PlaybackError(f"playback did not finish: {error}") from error
                    finally:
                        self._closed.set()
                        self._stream = None
                        self._converter = None
                        stream.close()


class Speaker:
    """글을 말한다(say 는 다 말할 때까지 기다린다). stop() 으로 바로 멈춘다.

    play 를 주면 스피커 대신 그 함수로 소리를 넘긴다(시험: 소리를 내지 않고 기록만). 주지 않으면 기본 출력 장치로 낸다.
    """

    def __init__(self, tts: TTSEngine, play: Callable[[np.ndarray, int, threading.Event], None] = _play,
                 lead_in: float = 0.0, lead_in_noise: float = 0.0, lead_in_tone: float = 0.0,
                 on_device_error: Callable[[], None] | None = None) -> None:
        self._tts = tts
        self._play = play
        self._on_device_error = on_device_error   # 재생 장치를 열지 못했을 때(장치를 다시 찾게 한 뒤 한 번 더 시도)
        self._lead_in = lead_in
        self._lead_in_noise = lead_in_noise
        self._lead_in_tone = lead_in_tone
        self._stopped = threading.Event()
        self._active = False
        self._last_output_at: float | None = None
        self.last_underflows = 0
        # 재생 직전에 그 소리를 넘겨받는 함수(화면 캐릭터의 입 움직임 등). 재생을 막지 않게 오류는 무시한다
        self.on_audio: Callable[[np.ndarray, int], None] | None = None
        # 재생한 문장과 그 시각. 말하는 도중 들은 소리가 자기 목소리인지 거르는 데 쓴다
        self._echo_recent: deque[tuple[float, str]] = deque(maxlen=40)
        self._echo_playing = ""
        # 방금 낸 소리의 크기 기록(소리 크기로 끼어들기). 재생 직전에 쌓는다
        self.reference = PlaybackReference()

    @property
    def tts(self) -> TTSEngine:
        return self._tts

    @tts.setter
    def tts(self, engine: TTSEngine) -> None:
        """말하기 엔진을 바꾼다(외국어 회화의 그 언어 목소리 등). 다음 say 부터 쓴다."""
        self._tts = engine

    def recent_echo_text(self) -> str:
        """재생 중인 문장과 재생이 끝난 직후의 문장들(자기 목소리 비교 자료)."""
        if self._last_output_at is None:
            return ""
        if not self.active and time.monotonic() - self._last_output_at > ECHO_TAIL_SECONDS:
            return ""
        since = time.monotonic() - RECENT_SECONDS
        texts = [text for at, text in list(self._echo_recent) if at >= since]
        if self._echo_playing:
            texts.append(self._echo_playing)
        return " ".join(dict.fromkeys(texts))

    @property
    def active(self) -> bool:
        """지금 말하는 중인지."""
        return self._active

    def stop(self) -> None:
        """말하던 것을 바로 멈춘다."""
        self._stopped.set()

    @property
    def stopping(self) -> bool:
        """멈추라는 요청을 받아 끝나가는 중인지(active 는 남은 정리가 끝나야 거짓이 된다)."""
        return self._stopped.is_set()

    def say(self, text: str, on_start: Callable[[], None] | None = None) -> float | None:
        """글을 다 말할 때까지 기다린다. 첫 소리가 나기까지 걸린 시간(초). 소리를 못 냈으면 None. 실패하면 TTSError."""
        sentences = split_sentences(text)
        if not sentences:
            return None
        self._stopped = stopped = threading.Event()   # 늦게 끝나는 합성 작업자가 다음 말의 멈춤 상태를 공유하지 않게
        self._active = True
        started = time.perf_counter()
        first_audio: float | None = None
        stream_audio = getattr(self._tts, "audio_stream", True)
        # 앞 문장을 재생하는 동안 다음 문장을 미리 만든다(200ms 조각 약 13초까지)
        ready: queue.Queue = queue.Queue(maxsize=64 if stream_audio else 2)
        continuous = (ContinuousPlayer(lead_in=self._lead_in, lead_in_noise=self._lead_in_noise,
                                       lead_in_tone=self._lead_in_tone, on_audio=self._observe_audio)
                      if self._play is _play else None)
        self.last_underflows = 0

        def put(item):
            while not stopped.is_set():
                try:
                    ready.put(item, timeout=0.05)
                    return
                except queue.Full:
                    pass

        def produce() -> None:
            try:
                for sentence in sentences:
                    if stopped.is_set():
                        break
                    stream = getattr(self._tts, "synthesize_stream", None) if stream_audio else None
                    if stream is None:
                        put((*self._tts.synthesize(sentence), sentence))
                        continue
                    chunks = stream(sentence, stopped)
                    try:
                        for item in chunks:
                            if stopped.is_set():
                                break
                            put((*item, sentence))
                    finally:
                        close = getattr(chunks, "close", None)
                        if close:
                            close()
            except Exception as e:
                put(e if isinstance(e, TTSError) else TTSError(f"{type(e).__name__}: {e}"))
            finally:
                put(None)   # 합성 작업자가 예외로 끝나도 재생 쪽이 큐에서 무한 대기하지 않게

        threading.Thread(target=produce, daemon=True, name="tts_produce").start()
        try:
            while not stopped.is_set():
                try:
                    item = ready.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, TTSError):
                    raise item
                audio, rate, sentence = item
                if first_audio is None:
                    if continuous is None:   # 시험용 출력 함수에는 첫 조각의 앞 무음을 여기서 붙인다
                        audio, added = with_lead_in(audio, rate, self._lead_in)
                    else:
                        added = max(0.0, self._lead_in)
                    first_audio = time.perf_counter() - started + added
                    if on_start is not None:
                        try:
                            on_start()
                        except Exception:
                            pass
                if not stopped.is_set():
                    self._last_output_at = time.monotonic()
                    self._echo_playing = sentence
                    self._play_with_retry(audio, rate, ready, play=continuous)
                    self._last_output_at = time.monotonic()
                    if self._echo_recent and self._echo_recent[-1][1] == sentence:
                        self._echo_recent.pop()
                    self._echo_recent.append((self._last_output_at, sentence))
                    self._echo_playing = ""
        finally:
            try:
                if continuous is not None:
                    continuous.close(abort=stopped.is_set())
                    self.last_underflows = continuous.underflows
            finally:
                if self._echo_playing:   # 오류·취소도 마지막으로 내보낸 문장의 꼬리는 비교해야 한다
                    self._last_output_at = time.monotonic()
                    self._echo_recent.append((self._last_output_at, self._echo_playing))
                    self._echo_playing = ""
                self._active = False
        return first_audio

    def _observe_audio(self, audio: np.ndarray, rate: int) -> None:
        """실제로 내보내는 소리를 화면(on_audio)과 끼어들기 기준(reference)에 함께 넘긴다."""
        if self.on_audio is not None:
            try:
                self.on_audio(audio, rate)
            except Exception:
                pass
        self.reference.add(audio, rate)

    def _play_with_retry(self, audio: np.ndarray, rate: int, ready: queue.Queue, play=None) -> None:
        """재생한다. 장치를 열지 못하면 장치를 다시 찾게 하고 한 번만 다시 시도한다. 또 실패하면 PlaybackError."""
        def attempt():
            if not isinstance(play, ContinuousPlayer):
                self._observe_audio(audio, rate)
            (play or self._play)(audio, rate, self._stopped)
        try:
            attempt()
            return
        except PlaybackError:
            if self._on_device_error is None:
                self._abort(ready)
                raise
        self._on_device_error()
        try:
            attempt()
        except PlaybackError:
            self._abort(ready)
            raise

    def _abort(self, ready: queue.Queue) -> None:
        self._stopped.set()
        while True:
            try:
                ready.get_nowait()
            except queue.Empty:
                return
