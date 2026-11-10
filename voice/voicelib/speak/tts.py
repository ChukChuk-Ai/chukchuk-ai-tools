"""음성 합성(TTS) 엔진. 설정의 순서대로 시도하고, 실패하면 다음 엔진으로 넘어간다.

엔진: edge(마이크로소프트 온라인 음성, edge-tts 패키지), windows(OS 내장 음성, os_audio.BuiltinVoice).
원본에 있던 Supertonic(로컬 엔진)은 라이선스를 확인하지 못해 뺐다. 오류 글은 짧은 영문(화면 글은 부르는 쪽이 만든다).
"""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Protocol

import numpy as np

from voicelib.os_audio import BuiltinVoice, BuiltinVoiceError
from voicelib.settings import TTSConfig

# 실패한 엔진은 이 시간 동안 건너뛴다. 매번 실패를 기다리느라 늦어지지 않게 한다
RETRY_AFTER_SECONDS = 60
EDGE_SAMPLE_RATE = 24000
EDGE_QUIET_RETRIES = 1  # 소리 없는 응답(NoAudioReceived)을 받았을 때 같은 글을 다시 보내는 횟수
EDGE_SLOW_START_SECONDS = 1.5  # 첫 수신부터 늦으면 해당 문장은 전부 모아 끊김 위험을 줄인다.


class TTSError(Exception):
    pass


class TTSEngine(Protocol):
    name: str

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """글을 소리로 만든다. (단일 채널 float32 소리, 샘플링 주파수)를 돌려준다."""
        ...


class EdgeTTS:
    """마이크로소프트의 온라인 음성. 읽을 글이 마이크로소프트 서버로 전송된다."""

    name = "edge"

    def __init__(self, voice: str) -> None:
        self._voice = voice

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        # 서버가 가끔 소리 없이 돌려보낸다(NoAudioReceived, 같은 글을 다시 보내면 되는 일시 오류. 실측: 한국어 문장 2번 중 1번).
        # 바로 한 번 더 시도하지 않으면 이 엔진이 쉬는 동안 다음 엔진(내장 음성)이 읽게 된다
        for attempt in range(1 + EDGE_QUIET_RETRIES):
            try:
                mp3 = asyncio.run(self._fetch(text))
                break
            except Exception as e:
                if type(e).__name__ == "NoAudioReceived" and attempt < EDGE_QUIET_RETRIES:
                    continue
                raise TTSError(f"edge-tts: {type(e).__name__}: {e}") from e
        if not mp3:
            raise TTSError("edge-tts: no audio")
        return _decode_mp3(mp3), EDGE_SAMPLE_RATE

    async def _fetch(self, text: str) -> bytes:
        import edge_tts

        data = bytearray()
        communicate = edge_tts.Communicate(text, self._voice, connect_timeout=5, receive_timeout=15)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data.extend(chunk["data"])
        return bytes(data)

    def synthesize_stream(self, text: str, stopped: threading.Event) -> Iterator[tuple[np.ndarray, int]]:
        """MP3 프레임을 이어서 해독한다. 첫 버퍼 뒤부터는 전체 수신을 기다리지 않는다."""
        ready: queue.Queue = queue.Queue(maxsize=4)
        closed = threading.Event()

        async def put(item):
            while not stopped.is_set() and not closed.is_set():
                try:
                    ready.put_nowait(item)
                    return True
                except queue.Full:
                    await asyncio.sleep(0.01)
            return False

        async def fetch():
            import edge_tts
            emitted = False
            try:
                for attempt in range(1 + EDGE_QUIET_RETRIES):
                    decoder = MP3StreamDecoder()
                    try:
                        begun = time.monotonic()
                        first_packet = True
                        communicate = edge_tts.Communicate(text, self._voice, connect_timeout=5, receive_timeout=15)
                        async for part in communicate.stream():
                            if stopped.is_set() or closed.is_set():
                                return
                            if part["type"] != "audio":
                                continue
                            if first_packet:
                                if time.monotonic() - begun > EDGE_SLOW_START_SECONDS:
                                    decoder.hold_until_complete()
                                first_packet = False
                            for audio in decoder.push(part["data"]):
                                emitted = True
                                if not await put((audio, EDGE_SAMPLE_RATE)):
                                    return
                        for audio in decoder.finish():
                            emitted = True
                            if not await put((audio, EDGE_SAMPLE_RATE)):
                                return
                        if not emitted:
                            raise TTSError("edge-tts: no audio")
                        break
                    except Exception as error:
                        if not emitted and type(error).__name__ == "NoAudioReceived" and attempt < EDGE_QUIET_RETRIES:
                            continue
                        raise
            except Exception as error:
                await put(error if isinstance(error, TTSError) else TTSError(f"edge-tts: {type(error).__name__}: {error}"))
            finally:
                await put(None)

        worker = threading.Thread(target=lambda: asyncio.run(fetch()), daemon=True)
        worker.start()
        try:
            while not stopped.is_set():
                try:
                    item = ready.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    return
                if isinstance(item, TTSError):
                    raise item
                yield item
        finally:
            closed.set()  # 다음 발화가 시작되어도 이전 수신 결과가 넘어오지 않는다.


class MP3StreamDecoder:
    """코덱/리샘플러 상태를 끝까지 보존하고 마지막 지연 프레임까지 배출한다."""

    def __init__(self) -> None:
        import av
        self._codec = av.CodecContext.create("mp3float", "r")
        self._resampler = av.AudioResampler(format="flt", layout="mono", rate=EDGE_SAMPLE_RATE)
        self._pending: list[np.ndarray] = []
        self._samples = 0
        self._first = True
        self._has_frames = False
        self._hold = False

    def hold_until_complete(self) -> None:
        self._hold = True

    def _frames(self, frames) -> Iterator[np.ndarray]:
        for frame in frames:
            self._has_frames = True
            for out in self._resampler.resample(frame):
                self._pending.append(out.to_ndarray()[0].copy())
                self._samples += out.samples
            # 처음에는450ms를 모아 네트워크 흔들림에 대비한다. 이후에는200ms씩 넘긴다.
            target = int(EDGE_SAMPLE_RATE * (0.45 if self._first else 0.2))
            if not self._hold and self._samples >= target:
                yield self._take()

    def _take(self) -> np.ndarray:
        audio = np.concatenate(self._pending)
        self._pending, self._samples, self._first = [], 0, False
        return audio

    def push(self, data: bytes) -> Iterator[np.ndarray]:
        for packet in self._codec.parse(data):
            yield from self._frames(self._codec.decode(packet))

    def finish(self) -> Iterator[np.ndarray]:
        for packet in self._codec.parse(b""):
            yield from self._frames(self._codec.decode(packet))
        yield from self._frames(self._codec.decode(None))
        if self._has_frames:
            for frame in self._resampler.resample(None):
                self._pending.append(frame.to_ndarray()[0].copy())
                self._samples += frame.samples
        if self._samples:
            yield self._take()


def _decode_mp3(data: bytes) -> np.ndarray:
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=EDGE_SAMPLE_RATE)
    chunks = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray()[0] for out in resampler.resample(frame))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")


class FallbackTTS:
    """엔진을 순서대로 시도한다. 방금 실패한 엔진은 잠시 건너뛴다."""

    name = "fallback"

    def __init__(self, engines: Sequence[TTSEngine]) -> None:
        if not engines:
            raise TTSError("no tts engine configured")
        self._engines = list(engines)
        self._failed_at: dict[str, float] = {}
        self.last_used = ""

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        result, _ = self.synthesize_with_source(text)
        return result

    def synthesize_with_source(self, text: str) -> tuple[tuple[np.ndarray, int], str]:
        """이번 합성에 쓰인 엔진 이름을 결과와 함께 돌려준다(동시 합성의 last_used와 분리)."""
        errors = []
        now = time.monotonic()
        # 쉬고 있는 엔진뿐이면 쉬는 것을 무시하고 전부 다시 시도한다
        resting = [e for e in self._engines if now - self._failed_at.get(e.name, -1e9) < RETRY_AFTER_SECONDS]
        candidates = [e for e in self._engines if e not in resting] or self._engines
        for engine in candidates:
            try:
                result = engine.synthesize(text)
            except TTSError as e:
                self._failed_at[engine.name] = time.monotonic()
                errors.append(str(e))
                continue
            self.last_used = engine.name
            return result, engine.name
        raise TTSError(" / ".join(errors))

    def synthesize_stream(self, text: str, stopped: threading.Event) -> Iterator[tuple[np.ndarray, int]]:
        now = time.monotonic()
        candidates = [e for e in self._engines if now - self._failed_at.get(e.name, -1e9) >= RETRY_AFTER_SECONDS] or self._engines
        errors = []
        for engine in candidates:
            if stopped.is_set():
                return
            emitted = False
            try:
                stream = getattr(engine, "synthesize_stream", None)
                chunks = stream(text, stopped) if stream else iter([engine.synthesize(text)])
                try:
                    for item in chunks:
                        if stopped.is_set():
                            return
                        emitted = True
                        self.last_used = engine.name
                        yield item
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()
                if stopped.is_set():
                    return
                if not emitted:
                    raise TTSError("no audio")
                return
            except TTSError as error:
                self._failed_at[engine.name] = time.monotonic()
                if emitted:
                    # 이미 말한 문장을 다른 엔진으로 처음부터 다시 읽지 않는다.
                    raise TTSError(f"stream interrupted: {error}") from error
                errors.append(str(error))
        raise TTSError(" / ".join(errors))


class WindowsTTS:
    """OS 내장 음성(os_audio.BuiltinVoice). 이름은 설정의 windows 를 그대로 쓴다(macOS 판도 같은 이름으로 OS 음성을 쓴다)."""

    name = "windows"

    def __init__(self, voice: str = "", language: str = "") -> None:
        self._voice = BuiltinVoice(voice, language)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        try:
            return self._voice.synthesize(text)
        except BuiltinVoiceError as e:
            raise TTSError(f"builtin voice: {e}") from e


def create_tts(config: TTSConfig, language: str = "") -> FallbackTTS:
    """설정의 엔진 순서대로 묶는다. edge 목소리는 settings 가 화면 언어로 채워 둔다."""
    engines: list[TTSEngine] = []
    for name in config.engines:
        if name == "edge":
            engines.append(EdgeTTS(config.edge_voice))
        elif name == "windows":
            engines.append(WindowsTTS(config.windows_voice, language))
        else:
            raise TTSError(f"unknown tts engine: {name}")
    tts = FallbackTTS(engines)
    tts.audio_stream = config.audio_stream
    return tts
