"""대화모드의 부분 받아쓰기. 녹음 스레드는 기다리지 않고 최신 스냅샷만 맡긴다."""
import threading
import time
import numpy as np


class StreamingDictation:
    def __init__(self, transcribe, on_text, on_error=lambda error: None, *, context=None, minimum_seconds=.3, interval_seconds=.2):
        self._transcribe, self._on_text, self._on_error = transcribe, on_text, on_error
        self._context = context
        self._lock = threading.Lock()
        self.configure(minimum_seconds, interval_seconds)
        self._wake, self._closed = threading.Event(), threading.Event()
        self._pending = None
        self._generation = 0
        self._last = (-1, 0)
        self._decode_seconds = 0.0
        self._decoding = False
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def submit(self, epoch, audio, activity=0):
        # 설정 간격마다 최신 사본만 교체한다. 추론 작업자는 한 개를 유지한다.
        if not self.wants(epoch, len(audio)):
            return
        self._last = (epoch, len(audio))
        with self._lock:
            self._pending = (epoch, np.array(audio, copy=True), self._generation, activity,
                             self._context() if self._context is not None else None)
        self._wake.set()

    def configure(self, minimum_seconds, interval_seconds):
        import math
        values = (minimum_seconds, interval_seconds)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               or not .1 <= value <= 2. for value in values):
            raise ValueError('부분 받아쓰기 시작·갱신 간격은0.1~2초여야 합니다.')
        with self._lock:
            self._minimum_samples = round(minimum_seconds * 16000)
            self._interval_samples = round(interval_seconds * 16000)

    def wants(self, epoch, samples):
        with self._lock:
            busy = self._decoding or self._pending is not None
            minimum, interval = self._minimum_samples, self._interval_samples
        interval = interval if busy else max(interval, int(self._decode_seconds * 16000))
        return samples >= minimum and (epoch != self._last[0] or samples - self._last[1] >= interval)

    def reset(self):
        with self._lock:
            self._generation += 1
            self._pending = None
        self._last = (-1, 0)

    def close(self):
        self.reset()
        self._closed.set()
        self._wake.set()

    def _work(self):
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
                self._decoding = item is not None
            if item is None:
                continue
            epoch, audio, generation, activity, context = item
            try:
                started = time.monotonic()
                text = self._transcribe(audio)
                self._decode_seconds = time.monotonic() - started
            except Exception as exc:
                with self._lock:
                    valid = generation == self._generation and not self._closed.is_set()
                if valid:
                    self._on_error(str(exc))
                continue
            finally:
                with self._lock:
                    self._decoding = False
            with self._lock:
                valid = generation == self._generation and not self._closed.is_set()
            if valid and text.strip():
                if self._context is None:
                    self._on_text(epoch, text, audio, activity)
                else:
                    self._on_text(epoch, text, audio, activity, context)
