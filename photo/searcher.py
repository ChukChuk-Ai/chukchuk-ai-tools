"""사진 찾기(search) 기능: analysis.json 기반 텍스트 매칭 및 썸네일 생성."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from PIL import Image, ImageOps

from common import TOOLS_DATA, T, load_config
from reporter import apply_rotation, path_link


def extract_search_query(text: str) -> str:
    """사용자가 한 말에서 검색어를 추출."""
    t = text.strip()
    patterns = [
        r"^(.*?)\s*나온\s*사진\s*(찾아줘|찾기|찾아|보여줘)?\s*$",
        r"^(.*?)\s*사진\s*(찾아줘|찾기|찾아|보여줘)\s*$",
        r"^(.*?)\s*(찾아줘|찾기|찾아)\s*$",
    ]
    for pat in patterns:
        m = re.match(pat, t)
        if m:
            cand = m.group(1).strip()
            if cand:
                t = cand
                break
    t = re.sub(r"\s*(?:사진|photos?|pictures?)\s*$", "", t, flags=re.I).strip()   # 영어 '... photos' 도
    return t


def tokenize_query(query: str) -> list[tuple[str, set[str]]]:
    """검색어를 단어 토큰과 후보 어근 집합(조사 제거)으로 분리."""
    raw_words = [w for w in re.split(r"\s+", query.strip()) if w]
    tokens = []
    particle_pat = re.compile(r"(이|가|을|를|의|에|에서|와|과|랑|도|은|는|앞|뒤|옆)$")
    for w in raw_words:
        stems = {w}
        stripped = particle_pat.sub("", w)
        if stripped and len(stripped) >= 1:
            stems.add(stripped)
        # "안고" -> "안" 등 보조 처리
        if w.endswith("고") and len(w) > 1:
            stems.add(w[:-1])
        if w.endswith("있는") and len(w) > 2:
            stems.add(w[:-2])
        tokens.append((w, stems))
    return tokens


def make_thumbnail(photo_path: Path, folder_name: str, rotate: int = 0, max_dim: int = 320, tools_data_dir: Path = TOOLS_DATA) -> Path:
    """긴 쪽 320px 썸네일을 data/tools/사진/<폴더>/thumbs/ 에 생성(EXIF 보정 + rotate 적용). 이미 있으면 재사용."""
    thumbs_dir = tools_data_dir / folder_name / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumbs_dir / f"{photo_path.stem}.jpg"
    if thumb_path.exists():
        return thumb_path

    if not photo_path.exists():
        return thumb_path

    with Image.open(photo_path) as img:
        img = ImageOps.exif_transpose(img)
        if rotate % 360 != 0:
            img = apply_rotation(img, rotate)
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(thumb_path, "JPEG", quality=85)
    return thumb_path


def search_photos(query: str, tools_data_dir: Path = TOOLS_DATA, cfg: dict | None = None) -> list[dict[str, Any]]:
    """analysis.json 들에서 query와 일치하는 사진 검색. 낱말이 많이 맞을수록 앞에. 최대 8장."""
    clean_query = extract_search_query(query)
    if not clean_query:
        clean_query = query.strip()
    tokens = tokenize_query(clean_query)
    if not tokens:
        return []

    if cfg is None:
        cfg = load_config()
    roots = [Path(r) for r in cfg.get("roots", [])]

    candidates = []
    # tools_data_dir 아래 모든 폴더의 analysis.json 탐색
    for analysis_file in tools_data_dir.glob("*/analysis.json"):
        folder_dir = analysis_file.parent
        try:
            with open(analysis_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        folder_name = data.get("folder", folder_dir.name)
        folder_path_str = data.get("folder_path", "")
        folder_path = Path(folder_path_str) if folder_path_str else None
        if not folder_path or not folder_path.exists():
            for root in roots:
                cand = root / folder_name
                if cand.exists():
                    folder_path = cand
                    break

        classification = data.get("classification", {})
        rotations = data.get("rotations", {})
        scenes = data.get("scenes", [])

        for sc in scenes:
            sc_idx = sc.get("scene_idx", 0)
            st = sc.get("story", {})
            story_title = st.get("title", "")
            story_text = st.get("text", "")
            story_full = f"{story_title} {story_text}"
            start_time = sc.get("start_time", "")

            analyses = sc.get("analyses", [])
            for an in analyses:
                fn = an.get("filename", "")
                if not fn:
                    continue

                people = an.get("people_present", [])
                action = an.get("action_and_mood", "")
                cls_info = classification.get(fn, {})
                place_guess = cls_info.get("place_guess", "")
                scene_keyword = cls_info.get("scene_keyword", "")
                place_full = f"{place_guess} {scene_keyword}"
                rotate = an.get("rotate", rotations.get(fn, cls_info.get("rotate", 0)))

                people_str = " ".join(people)
                full_text = f"{people_str} {action} {story_full} {place_full}"

                # 낱말 매칭 판정
                matched_words = 0
                people_hits = 0
                action_hits = 0
                total_freq = 0

                for word, stems in tokens:
                    hit = False
                    for stem in stems:
                        # 텍스트에 포함되어 있는지 확인
                        if stem in full_text:
                            hit = True
                            total_freq += full_text.count(stem)
                        if stem in people_str:
                            people_hits += 1
                        if stem in action:
                            action_hits += 1
                    if hit:
                        matched_words += 1

                if matched_words == 0:
                    continue

                # 원본 파일 경로
                p_file = (folder_path / fn) if folder_path else (folder_dir / fn)

                candidates.append({
                    "folder": folder_name,
                    "folder_path": folder_path,
                    "filename": fn,
                    "file_path": p_file,
                    "time_str": start_time,
                    "action_desc": action or story_title,
                    "people": people,
                    "rotate": rotate,
                    "matched_words": matched_words,
                    "people_hits": people_hits,
                    "action_hits": action_hits,
                    "total_freq": total_freq,
                    "scene_idx": sc_idx,
                })

    if not candidates:
        return []

    # 낱말이 많이 맞을수록 앞에, 그 다음 인물 일치, 행동 일치, 빈도 순
    candidates.sort(
        key=lambda c: (c["matched_words"], c["people_hits"], c["action_hits"], c["total_freq"]),
        reverse=True,
    )

    selected = candidates[:8]
    # 썸네일 생성
    for item in selected:
        thumb = make_thumbnail(
            item["file_path"],
            item["folder"],
            rotate=item["rotate"],
            max_dim=320,
            tools_data_dir=tools_data_dir,
        )
        item["thumb_path"] = thumb

    return selected


def format_search_card(query: str, results: list[dict[str, Any]]) -> str:
    """사진 찾기 마크다운 카드 생성."""
    clean_query = extract_search_query(query)
    if not clean_query:
        clean_query = query.strip()

    if not results:
        return T("search_none", query=clean_query)

    folders = sorted(set(r["folder"] for r in results))
    lines = [
        T("search_title", query=clean_query),
        "",
        T("search_summary", folders=len(folders), n=len(results)),
        "",
    ]

    for r in results:
        thumb_path = r.get("thumb_path")
        folder = r["folder"]
        filename = r["filename"]
        time_str = r.get("time_str", "")
        desc = r.get("action_desc", "").replace("\n", " ").strip()
        if len(desc) > 80:
            desc = desc[:77] + "..."

        meta_parts = [f"**{folder}** `{filename}`"]
        if time_str:
            meta_parts.append(time_str)
        if desc:
            meta_parts.append(desc)
        meta_line = " · ".join(meta_parts)

        p_link = path_link(r["file_path"])

        if thumb_path and Path(thumb_path).exists():
            lines.append(f"![]({thumb_path})")
        lines.append(f"- {meta_line}")
        lines.append(T("search_open", link=p_link))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
