"""말소리 검출(VAD). faster-whisper 에 들어 있는 Silero 모델을 프레임 단위로 돌린다."""

from __future__ import annotations

import numpy as np

from voicelib.listen import FRAME_SAMPLES

CONTEXT_SAMPLES = 64


class StreamingVAD:
    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros(CONTEXT_SAMPLES, dtype="float32")

    def probability(self, frame: np.ndarray) -> float:
        """프레임(512 샘플) 하나가 말소리일 확률."""
        if len(frame) != FRAME_SAMPLES:
            raise ValueError(f"프레임 길이는 {FRAME_SAMPLES} 이어야 합니다: {len(frame)}")
        inputs = np.concatenate([self._context, frame])[None].astype("float32")
        output, self._h, self._c = self._session.run(None, {"input": inputs, "h": self._h, "c": self._c})
        self._context = frame[-CONTEXT_SAMPLES:]
        return float(output.flatten()[0])
