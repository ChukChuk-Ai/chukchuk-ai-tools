"""캐릭터가 방금 낸 소리의 크기 기록(말하는 도중 끼어들기용, 2026-10-18).

재생하기 직전에 소리를 넘겨받아 0.032초 프레임마다 크기(dBFS)를 구해 두고, 재생 시작 시각에 놓는다.
"최근 window 초 안에 낸 소리 중 가장 큰 크기"를 물어볼 수 있다. 스피커 소리가 마이크에 들어오기까지의 지연이
정확하지 않아도(0.3~1초로 측정됨) 안전하도록 한 시점이 아니라 창 안의 최댓값을 쓴다.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

import numpy as np

from voicelib.audio_level import FLOOR_DB, frame_db, to_16k
from voicelib.listen import FRAME_SECONDS


class PlaybackReference:
    def __init__(self, keep_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._keep = keep_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._segments: list[tuple[float, np.ndarray]] = []  # (재생 시작 시각, 프레임별 dB)

    def add(self, audio: np.ndarray, rate: int, start: float | None = None) -> None:
        """재생하려는 소리를 기록한다. start 를 주지 않으면 이 함수가 끝난 시각을 재생 시작으로 본다(바로 재생하므로)."""
        db = frame_db(to_16k(audio, rate))
        stamp = self._clock() if start is None else start
        with self._lock:
            self._segments.append((stamp, db))
            horizon = stamp - self._keep
            while self._segments and self._segments[0][0] + len(self._segments[0][1]) * FRAME_SECONDS < horizon:
                self._segments.pop(0)

    def recent_max_db(self, window: float, now: float | None = None) -> float:
        """now 로부터 window 초 안(지금까지 재생이 시작된 부분)에 낸 소리의 가장 큰 프레임 크기. 없으면 FLOOR_DB."""
        now = self._clock() if now is None else now
        best = FLOOR_DB
        with self._lock:
            for start, db in self._segments:
                lo = math.floor((now - window - start) / FRAME_SECONDS)
                hi = math.floor((now - start) / FRAME_SECONDS)
                lo, hi = max(lo, 0), min(hi, len(db) - 1)
                if hi >= lo:
                    best = max(best, float(db[lo:hi + 1].max()))
        return best

    def clear(self) -> None:
        with self._lock:
            self._segments.clear()
