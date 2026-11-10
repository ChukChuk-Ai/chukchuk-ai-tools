"""시험용 가짜 녹음 장치(sounddevice 의 query_devices·RawInputStream 자리). wav 를 48kHz int16 으로 실제 시간에 맞춰 흘려 보낸다."""
import threading
import time
import wave

import numpy as np


class FakeBackend:
    def __init__(self, paths, rate=48000):
        chunks = []
        for path in paths:
            with wave.open(str(path), "rb") as w:
                src_rate, raw = w.getframerate(), w.readframes(w.getnframes())
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            idx = np.arange(0, len(audio), src_rate / rate).astype(int)
            chunks += [audio[idx[idx < len(audio)]], np.zeros(rate // 2, dtype=np.float32)]
        self.audio = np.concatenate(chunks).astype(np.int16)
        self.rate = rate

    def query_devices(self, kind=None):
        return {"default_samplerate": self.rate, "max_input_channels": 1, "name": "fake mic"}

    def RawInputStream(self, samplerate, channels, dtype, blocksize, callback):  # noqa: N802
        return _Stream(self.audio, samplerate, blocksize, callback)


class _Stream:
    def __init__(self, audio, rate, block, callback):
        self.audio, self.rate, self.block, self.callback = audio, rate, block, callback
        self.active = False
        self._stop = threading.Event()

    def __enter__(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        pos = 0
        while not self._stop.is_set():
            data = self.audio[pos:pos + self.block]
            if len(data) < self.block:
                data = np.concatenate([data, np.zeros(self.block - len(data), dtype=np.int16)])
            pos += self.block
            self.callback(data.tobytes(), self.block, None, None)
            time.sleep(self.block / self.rate)

    def __exit__(self, *exc):
        self._stop.set()
        self.active = False
