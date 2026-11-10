"""사진 도구 공통: 폴더와 설정(sdk), 결과 글(texts.toml). 설정 값은 환경설정 > 도구 설정(tool.json 의 settings)에서 고친다."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_ROOT = os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1])   # sdk 찾기(앱 폴더는 앱이 알려 준다)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sdk import config, data, text  # noqa: E402

DATA_DIR = data.DATA
TOOLS_DATA = data.tool_dir("photo")    # 분석 결과·썸네일·작업 상태
MYDATA_DIR = data.mydata("photo")      # 만든 이야기책(HTML)
MODEL = "image"                        # 작업 분야 '사진 보기'(환경설정 > 작업 분야별 모델)
T = text.load(HERE)                    # 결과 글(화면 언어). 모듈마다 from common import T
LANG_NAMES = {"ko": "한국어", "en": "English"}   # 모델이 쓸 글의 언어(화면 언어)
GUESS = re.compile(r"\s*\((?:추정|guess)\)", re.I)   # 호칭 짐작 표시: 한국어 '(추정)', 영어 ' (guess)'


def load_config() -> dict[str, Any]:
    cfg = config.tool_settings(HERE)
    cfg["sort_model"] = cfg["story_model"] = MODEL   # 원본의 두 모델(분류·이야기)은 한 분야 모델로
    return cfg


def output_language(what: str = "호칭·특징·설명·제목·이야기") -> str:
    """프롬프트 끝에 붙이는 한 줄: 모델이 쓰는 글을 화면 언어로. 한국어면 지금까지와 같은 결과다."""
    lang = config.language()
    line = f"모든 글({what})은 {LANG_NAMES.get(lang, 'English')}로 쓰세요."
    if lang != "ko":   # 프롬프트의 한국어 예시·호칭·'(추정)'을 그 언어로 옮겨 쓰게 한다
        line += (" 위 지침의 한국어 예시·문체 지시보다 이 줄을 따르고, 가족 호칭도 그 언어로, '(추정)'은 ' (guess)'로,"
                 " '손님들'은 'Guests'로 쓰세요(예: 엄마(추정) → Mom (guess)).")
    return line


def strip_guess(name: str) -> str:
    """'엄마(추정)'·'Mom (guess)' → '엄마'·'Mom'."""
    return GUESS.sub("", name).strip()
