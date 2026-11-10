"""사진 폴더 검색 모듈."""
from __future__ import annotations

from pathlib import Path


def find_photo_folder(folder_query: str, roots: list[str]) -> tuple[Path | None, list[Path]]:
    """폴더를 전체 경로 또는 roots 아래(1단계 하위까지)에서 찾는다.

    Returns:
        (단일 매칭 경로, []) : 1개만 찾음
        (None, [후보들])    : 2개 이상 찾음
        (None, [])          : 못 찾음
    """
    query = folder_query.strip().strip('"').strip("'")
    if not query:
        return None, []

    # 1. 전체 경로인지 확인
    direct_path = Path(query)
    try:
        if direct_path.is_dir():
            return direct_path.resolve(), []
    except Exception:
        pass

    target_name = direct_path.name if direct_path.parts else query
    target_lower = target_name.lower()

    exact_matches: list[Path] = []
    partial_matches: list[Path] = []

    for root_str in roots:
        root_path = Path(root_str)
        try:
            if not root_path.is_dir():
                continue
        except Exception:
            continue

        try:
            for child in root_path.iterdir():
                try:
                    if child.is_dir():
                        c_lower = child.name.lower()
                        if c_lower == target_lower:
                            exact_matches.append(child)
                        elif target_lower in c_lower:
                            partial_matches.append(child)

                        # 한 단계 하위까지 탐색
                        try:
                            for sub in child.iterdir():
                                if sub.is_dir():
                                    s_lower = sub.name.lower()
                                    if s_lower == target_lower:
                                        exact_matches.append(sub)
                                    elif target_lower in s_lower:
                                        partial_matches.append(sub)
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            continue

    # 중복 제거
    def dedupe(paths: list[Path]) -> list[Path]:
        seen = set()
        res = []
        for p in paths:
            norm = str(p.resolve()).lower()
            if norm not in seen:
                seen.add(norm)
                res.append(p)
        return res

    exact_matches = dedupe(exact_matches)
    partial_matches = dedupe(partial_matches)

    candidates = exact_matches if exact_matches else partial_matches
    if len(candidates) == 1:
        return candidates[0], []
    if len(candidates) > 1:
        return None, candidates
    return None, []
