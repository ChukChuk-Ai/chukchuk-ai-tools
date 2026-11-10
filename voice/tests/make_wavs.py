"""시험용 말소리 파일 만들기: edge 음성으로 문장을 읽혀 16kHz wav 로 저장한다(스피커로 내지 않는다).
"SIL:3" 은 조용한 소리 3초."""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voicelib.speak.tts import EdgeTTS  # noqa: E402


def make(said: str, path: Path, voice: str = "ko-KR-SunHiNeural") -> Path:
    if said.startswith("SIL:"):
        audio, rate = np.zeros(int(float(said[4:]) * 16000), dtype=np.float32), 16000
    else:
        audio, rate = EdgeTTS(voice).synthesize(said)
    idx = np.arange(0, len(audio), rate / 16000).astype(int)
    audio = np.clip(audio[idx[idx < len(audio)]], -1, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1])
    for i, said in enumerate(sys.argv[2:]):
        voice = "en-US-AriaNeural" if said.startswith("EN:") else "ko-KR-SunHiNeural"
        make(said.removeprefix("EN:"), out / f"{i:02d}.wav", voice)
