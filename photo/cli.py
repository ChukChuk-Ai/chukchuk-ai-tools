"""모델 부르기: 작업 분야 '사진 보기'(환경설정 > 작업 분야별 모델)로 sdk.llm 을 부른다. 실패하면 한 번 다시 시도한다.
사진은 base64 image 블록을 그림 목록으로 바꿔 넘긴다. 토큰 수는 sdk 가 돌려주지 않아 0 으로 둔다.
"""
from __future__ import annotations

import common  # sdk 를 찾게 하고, 기본 작업 분야를 준다
from sdk import llm

TIMEOUT = 600
RETRIES = 1
CliError = llm.LLMError


def ask(model: str = common.MODEL, content: list[dict] | None = None) -> tuple[str, dict]:
    """content(text·image 블록 목록)를 보내 (답 글, 토큰 수)를 돌려준다. model 은 작업 분야 이름(예: "image")."""
    content = content or []
    images = [(b["source"]["media_type"], b["source"]["data"]) for b in content if b.get("type") == "image"]
    text = "\n\n".join(b["text"] for b in content if b.get("type") == "text")
    last: Exception | None = None
    for _ in range(RETRIES + 1):
        try:
            return llm.ask(model, text, images=images, timeout=TIMEOUT, max_tokens=16000), {"input_tokens": 0, "output_tokens": 0}
        except llm.LLMError as e:
            last = e
    raise last  # type: ignore[misc]


def ask_text(model: str, prompt: str) -> tuple[str, dict]:
    return ask(model, [{"type": "text", "text": prompt}])
