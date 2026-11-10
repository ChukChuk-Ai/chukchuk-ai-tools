"""마이크 입력 부품. 원본 AI_Partner voice_session.py 의 safe_refresh_portaudio·MicrophoneFrames·MicWatch 와 상수,
voice.py 의 '호출 대기 중 버릴 소리' 판단(drop_reason)을 옮겼다.

알림 글은 만들지 않고 짧은 영문 코드(reason)만 남긴다. 화면 글은 부르는 쪽(voice.py)이 texts.toml 로 만든다.
"""
from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from voicelib.audio_devices import refresh_portaudio
from voicelib.listen import FRAME_SAMPLES, SAMPLE_RATE
from voicelib.listen.endpoint import PRE_ROLL_SECONDS, Utterance

NO_FRAMES_SECONDS = 3.0
RECONNECT_BACKOFF_BASE = 1.0
RECONNECT_BACKOFF_MAX = 30.0
HEALTHY_SECONDS = 5.0
INPUT_BUFFER_FRAMES = math.ceil(10 * SAMPLE_RATE / FRAME_SAMPLES)


def safe_refresh_portaudio() -> bool:
    """PortAudio 장치 목록을 새로 읽는다. 소리 서비스가 없을 때(원격 데스크톱 세션이 끊긴 동안 등) 나는 오류는 삼키고 실패를 알린다."""
    try:
        refresh_portaudio()
        return True
    except Exception:
        return False


class MicrophoneFrames:
    """장치 콜백을 기다리게 하지 않는 유한 입력 큐. 누락되면 failed 를 세우고 reason 에 코드를 남긴다(다시 연결)."""

    def __init__(self, capacity=INPUT_BUFFER_FRAMES, clock=time.monotonic):
        if not 1 <= capacity <= INPUT_BUFFER_FRAMES:
            raise ValueError(f"capacity must be 1..{INPUT_BUFFER_FRAMES}")
        self._frames = queue.Queue(maxsize=capacity)
        self.failed = threading.Event()
        self.reason = ""
        self._clock = clock
        self.last_captured_at = None

    def on_audio(self, indata, frame_count, time_info, status):
        if self.failed.is_set():
            return
        if status:
            self.reason = "mic_overflow"
        elif frame_count != FRAME_SAMPLES or indata.shape != (FRAME_SAMPLES, 1) or not np.isfinite(indata).all():
            self.reason = "mic_bad_format"
        else:
            frame = indata[:, 0].copy()
            try:
                self._frames.put_nowait((self._clock(), frame))
                return
            except queue.Full:
                frame.fill(0)
                self.reason = "mic_behind"
        self.failed.set()

    def get(self, timeout):
        self.last_captured_at, frame = self._frames.get(timeout=timeout)
        return frame

    def clear(self):
        for _ in range(self._frames.qsize()):
            try:
                self._frames.get_nowait()[1].fill(0)
            except queue.Empty:
                break


class MicWatch:
    """마이크에서 소리가 들어오는지 살핀다.

    잭에 기기를 꽂으면 기본 입력이 아무것도 연결되지 않은 마이크로 바뀌기도 한다. 그러면 아무 반응 없이 듣기만 하므로,
    5초 동안 소리가 거의 0 이면 notify("mic_silent", 최대값), 다시 들어오면 notify("mic_back", 최대값)을 부른다.
    """

    WINDOW_SECONDS = 5.0
    SILENT_PEAK = 0.001  # 조용한 방의 실제 마이크도 이보다는 크다. 꺼진 입력은 0 에 가깝다

    def __init__(self, notify: Callable[[str, float], None]) -> None:
        self._notify = notify
        self._peak = 0.0
        self._seconds = 0.0
        self._warned = False

    def frame(self, frame: np.ndarray) -> bool:
        """프레임을 넣는다. 한 구간(5초)이 끝났는데 소리가 정확히 0 이면 True (장치가 죽었을 수 있다)."""
        self._peak = max(self._peak, float(np.abs(frame).max(initial=0.0)))
        self._seconds += len(frame) / SAMPLE_RATE
        if self._seconds < self.WINDOW_SECONDS:
            return False
        dead = self._peak == 0.0
        if self._peak < self.SILENT_PEAK and not self._warned:
            self._warned = True
            self._notify("mic_silent", self._peak)
        elif self._peak >= self.SILENT_PEAK and self._warned:
            self._warned = False
            self._notify("mic_back", self._peak)
        self._peak, self._seconds = 0.0, 0.0
        return dead


def drop_reason(utterance: Utterance, submitted: float | None, behind: int, *, awaiting_wake: bool,
                max_pending: int, stale_seconds: float, min_speech: float, clock: Callable[[], float] = time.monotonic) -> str | None:
    """호출 대기 중 받아 적지 않고 버릴 소리면 그 이유 코드(behind·stale·short). 호출된 대화 중이면 None."""
    if not awaiting_wake:
        return None
    if max_pending and behind >= max_pending:
        return "behind"
    if stale_seconds and submitted is not None and clock() - submitted > stale_seconds:
        return "stale"
    speech = len(utterance.audio) / SAMPLE_RATE - utterance.waited - PRE_ROLL_SECONDS
    if min_speech and speech < min_speech:
        return "short"
    return None
