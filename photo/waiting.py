"""사진 이야기책 대기 줄(data\\tools\\사진\\queue.json): 여러 폴더를 한 번에 시키거나 작업 중에 시키면 줄에 올린다.
(이름이 queue 가 아닌 이유: 파이썬 기본 모듈 queue 를 가리지 않으려고)"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from common import TOOLS_DATA

QUEUE_FILE = TOOLS_DATA / "queue.json"


def split_folders(text: str) -> list[str]:
    """'여행사진1, 여행사진2, 바다' → 세 폴더. 쉼표(,、)로만 나눈다. 와/과/하고 는 split_by_particle 로 따로."""
    return [p.strip() for p in re.split(r"\s*[,、，]\s*", text or "") if p.strip()]


def split_by_particle(text: str) -> list[str]:
    """'여행사진1과 여행사진2' → 둘. 폴더 이름 자체에 들어 있는 '과' 와 헷갈릴 수 있어, 부르는 쪽이 각 조각이 실제 폴더일 때만 쓴다."""
    parts = re.split(r"(?<=\S)(?:이랑|하고|랑|와|과)\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def strip_ending(name: str) -> str:
    """끝의 조사(로/으로/에서/의)를 뗀다. '여행사진2로' → '여행사진2'."""
    return re.sub(r"(?:으로|에서|로|의)$", "", name.strip())


def split_names(text: str, found) -> list[str]:
    """말 속의 폴더 이름 목록. 쉼표, 와/과/하고/랑/이랑 으로 나누고 끝의 조사를 뗀다.
    found(이름) 이 참인 조각으로만 나눈다 — 아니면 그 부분은 원래 말 그대로 한 이름으로 둔다(이름 속의 '과' 등을 지킨다)."""
    result: list[str] = []
    for piece in split_folders(text):
        if found(piece):
            result.append(piece)
            continue
        parts = split_by_particle(piece)
        parts = [p if found(p) else strip_ending(p) for p in parts]
        if len(parts) > 1 and all(found(p) for p in parts):
            result.extend(parts)
            continue
        stripped = strip_ending(piece)
        result.append(stripped if found(stripped) else piece)
    return result


def load_queue() -> list[dict]:
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_queue(items: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue(folder_path: str | Path, photos: int = 0) -> int:
    """줄에 올리고 줄에서의 순서(1부터)를 돌려준다. 이미 줄에 있으면 그 순서."""
    items = load_queue()
    path = str(folder_path)
    for i, it in enumerate(items):
        if it.get("folder_path") == path:
            return i + 1
    items.append({"folder_path": path, "name": Path(path).name, "photos": photos,
                  "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    save_queue(items)
    return len(items)


def pop_next() -> dict | None:
    """줄 맨 앞을 꺼낸다. 없으면 None."""
    items = load_queue()
    if not items:
        return None
    first = items.pop(0)
    save_queue(items)
    return first


def clear_queue() -> int:
    """줄을 비우고 비운 개수를 돌려준다."""
    n = len(load_queue())
    if n:
        save_queue([])
    return n
