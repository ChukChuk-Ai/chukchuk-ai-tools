"""출력 장치 열기: 기본 출력이 안 되는 장치는 지원 형식의 콜백 출력으로 연다. 샘플링 주파수 변환. (원본 AI_Partner speak/output.py)"""
from collections import deque
from contextlib import ExitStack
import threading
import time
import numpy as np


class CallbackOutput:
    """오디오 콜백에는 준비된 PCM만 넘긴다. 생산자는 제한된 큐에서 기다린다."""
    def __init__(self, sd, rate, *, startup_frames=0):
        self._sd = sd
        self.rate = rate
        self._cv = threading.Condition()
        self._queue = deque()
        self._pending = self._offset = self._frames = 0
        self._aborted = False
        self.underflows = 0
        self._error = None
        self._stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                       latency="high", callback=self._callback)

    @property
    def latency(self):
        return self._stream.latency

    def start(self):
        self._stream.start()

    def _callback(self, out, frames, stamp, status):
        out.fill(0)
        try:
            if status.output_underflow:
                self.underflows += 1
            dest = 0
            with self._cv:
                while dest < frames and self._queue:
                    data = self._queue[0]
                    count = min(frames - dest, len(data) - self._offset)
                    part = data[self._offset:self._offset + count]
                    out[dest:dest + count, 0] = part
                    dest += count
                    self._offset += count
                    self._pending -= count
                    self._frames += count
                    if self._offset == len(data):
                        self._queue.popleft()
                        self._offset = 0
                self._cv.notify_all()
        except Exception as exc:
            with self._cv:
                self._error = exc
                self._cv.notify_all()

    def write(self, audio):
        deadline = time.monotonic() + 5
        with self._cv:
            while self._pending >= self.rate * .3 and not self._aborted and self._error is None:
                if time.monotonic() >= deadline:
                    raise self._sd.PortAudioError("output callback not responding")
                self._cv.wait(.03)
            if self._error is not None:
                raise self._sd.PortAudioError("output callback stopped") from self._error
            if not self._aborted:
                data = np.ascontiguousarray(audio, dtype="float32").reshape(-1).copy()
                self._queue.append(data)
                self._pending += len(data)
        return False

    def stop(self):
        deadline = time.monotonic() + 5
        with self._cv:
            while self._pending and not self._aborted and self._error is None:
                if time.monotonic() >= deadline:
                    raise self._sd.PortAudioError("output buffer not played")
                self._cv.wait(.03)
            if self._error is not None:
                raise self._sd.PortAudioError("output callback stopped") from self._error
        self._stream.stop()

    def abort(self):
        with self._cv:
            self._aborted = True
            self._queue.clear()
            self._pending = 0
            self._cv.notify_all()
        self._stream.abort()

    def close(self):
        self._stream.close()


class RateConverter:
    """조각 경계의 필터 상태와 마지막 지연 샘플을 보존한다."""
    def __init__(self, source_rate, target_rate):
        import av
        self.source_rate = source_rate
        self._av = av
        self._resampler = av.AudioResampler(format="flt", layout="mono", rate=target_rate)

    def convert(self, audio=None):
        frame = None
        if audio is not None:
            frame = self._av.AudioFrame.from_ndarray(np.ascontiguousarray(audio, dtype="float32").reshape(1, -1),
                                                     format="flt", layout="mono")
            frame.sample_rate = self.source_rate
        frames = self._resampler.resample(frame)
        return np.concatenate([x.to_ndarray()[0] for x in frames]) if frames else np.empty(0, dtype="float32")


_last_speaker: list = []   # 지난번에 연 기본 스피커 식별자(처음엔 비어 있음)


def _refresh_if_speaker_changed() -> None:
    """기본 스피커가 바뀌었으면(원격 연결 등) 장치 목록을 새로 읽는다. 마이크가 꺼져 있어도 말하기 전에 맞춘다.
    마이크가 열려 있으면 마이크 쪽이 이미 바뀐 것을 보고 새로 읽으므로, 사용 중이면 기다리지 않고 넘어간다."""
    from voicelib.audio_devices import default_device_ids, refresh_portaudio  # 기본 장치 식별자는 os_audio

    try:
        speaker = default_device_ids()[1]
    except Exception:
        return
    if _last_speaker and _last_speaker[0] != speaker:
        try:
            refresh_portaudio(timeout=0.5)
        except Exception:
            pass
    _last_speaker[:] = [speaker]


def open_output(sd, source_rate, lead_in):
    """장치 생성·형식 협상부터 close 완료까지 PortAudio 재초기화를 막는다."""
    from voicelib.audio_devices import portaudio_use, refresh_portaudio

    _refresh_if_speaker_changed()
    for attempt in (1, 2):
        try:
            with ExitStack() as lifetime:
                lifetime.enter_context(portaudio_use())
                stream, rate = _open_output(sd, source_rate, lead_in)
                return _OwnedOutput(stream, lifetime.pop_all()), rate
        except sd.PortAudioError:
            if attempt == 2:
                raise
            try:   # 장치 목록이 옛것이라 못 열었을 수 있다: 한 번 새로 읽고 다시 연다
                refresh_portaudio(timeout=0.5)
            except Exception:
                pass


class _OwnedOutput:
    def __init__(self, stream, lifetime):
        self._stream = stream
        self._lifetime = lifetime
        self._close_lock = threading.Lock()
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._lifetime.close()


def _open_output(sd, source_rate, lead_in):
    """기존 출력 우선. 실패하면 같은 기본 장치에서 지원 형식의 콜백 출력을 연다."""
    stream = None
    try:
        stream = sd.OutputStream(samplerate=source_rate, channels=1, dtype="float32", latency="high")
        stream.start()
        return stream, source_rate
    except sd.PortAudioError:
        if stream is not None:
            stream.close()
    info = sd.query_devices(kind="output")
    rates = dict.fromkeys((source_rate, 48000, int(info["default_samplerate"]), 44100))
    for rate in rates:
        candidate = None
        try:
            sd.check_output_settings(samplerate=rate, channels=1, dtype="float32")
            candidate = CallbackOutput(sd, rate, startup_frames=round(lead_in * rate))
            candidate.start()
            return candidate, rate
        except sd.PortAudioError as exc:
            last = exc
            if candidate is not None:
                candidate.close()
    raise last
