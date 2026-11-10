"""음성 도구 시험. 실제 마이크·스피커는 쓰지 않는다: 말소리는 edge 음성으로 만든 wav 를 마이크 대신 흘려 넣고(--input),
말하기는 소리를 내지 않고 기록만 한다(--no-sound).

실행(앱 폴더에서, 도구 환경의 파이썬으로): shared_tools\\voice\\.venv\\Scripts\\python.exe -m pytest shared_tools\\voice\\tests -q
받아쓰기 모델(small, 약 470MB)과 edge 음성(인터넷)이 필요한 시험은 쓸 수 없으면 건너뛴다.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
ROOT = TOOL.parents[1]
os.environ.setdefault("CHUKCHUK_ROOT", str(ROOT))
sys.path[:0] = [str(TOOL), str(HERE), str(ROOT)]


@pytest.fixture(scope="session")
def wavs(tmp_path_factory):
    """wavs("이름", "문장", "EN:english", "SIL:3") → wav 파일 목록. edge 음성을 쓸 수 없으면 건너뛴다."""
    from make_wavs import make
    base = tmp_path_factory.mktemp("wav")

    def build(name, *texts):
        folder = base / name
        try:
            for i, said in enumerate(texts):
                voice = "en-US-AriaNeural" if said.startswith("EN:") else "ko-KR-SunHiNeural"
                make(said.removeprefix("EN:"), folder / f"{i:02d}.wav", voice)
        except Exception as e:
            pytest.skip(f"edge 음성으로 시험 말소리를 만들지 못했습니다(인터넷?): {type(e).__name__}")
        return sorted(folder.glob("*.wav"))
    return build
