"""브리핑 도구가 함께 쓰는 것: sdk 경로, 글(texts.toml), 실패."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1]))   # sdk 찾기(앱 폴더는 앱이 알려 준다)
from sdk import text  # noqa: E402

T = text.load(HERE)


class Fail(Exception):
    """그 단락만 '확인 못 함: 이유'로 보인다."""
