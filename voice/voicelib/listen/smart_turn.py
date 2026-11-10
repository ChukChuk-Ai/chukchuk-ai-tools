"""Smart Turn: 멈춘 직후의 소리(억양)로 말이 끝났는지 판단하는 로컬 모델.

모델 출처: Hugging Face 저장소 pipecat-ai/smart-turn-v3 의 smart-turn-v3.2-cpu.onnx(약 9MB),
라이선스 BSD 2-Clause(모델 카드 기준). 처음 쓸 때 받아 data/tools/voice/models/smart-turn/ 에 둔다.
받지 못하면(인터넷 없음 등) 부르는 쪽이 이 모델 없이 침묵 길이만으로 말 끝을 판단한다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from voicelib.listen import SAMPLE_RATE

REPO = "pipecat-ai/smart-turn-v3"
MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
WINDOW_SECONDS = 8  # 모델이 보는 길이. 마지막 8초를 쓰고, 짧으면 앞을 0 으로 채운다


def is_installed(model_dir: Path) -> bool:
    return (Path(model_dir) / MODEL_FILE).is_file()


class SmartTurn:
    def __init__(self, model_dir: Path, *, local_files_only: bool = False) -> None:
        path = Path(model_dir) / MODEL_FILE
        if local_files_only and not path.is_file():
            raise FileNotFoundError("smart_turn_missing")
        import onnxruntime as ort
        from faster_whisper.feature_extractor import FeatureExtractor

        if not path.is_file():
            from huggingface_hub import hf_hub_download
            path = Path(hf_hub_download(REPO, MODEL_FILE, local_dir=model_dir))
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self._features = FeatureExtractor(feature_size=80, chunk_length=WINDOW_SECONDS)

    def complete_probability(self, audio: np.ndarray) -> float:
        """지금까지의 소리를 보고 말이 끝났을 확률을 돌려준다."""
        size = WINDOW_SECONDS * SAMPLE_RATE
        audio = audio[-size:]
        if len(audio) < size:
            audio = np.pad(audio, (size - len(audio), 0))
        audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
        features = self._features(audio.astype(np.float32), padding=0)[None].astype(np.float32)
        return float(self._session.run(None, {"input_features": features})[0].flatten()[0])
