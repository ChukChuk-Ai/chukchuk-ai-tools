"""오늘의 추억: 만든 이야기책의 사진 중 오늘과 같은 날(월·일)에 찍은 사진을 먼저, 없으면 아무 사진이나 한 장을 겹치지 않게 골라 카드로.

AI Jarvis 커밋 71a9229 의 tools/오늘의추억/run.py 를 옮겼다(사진 도구의 명령으로, 결정 15).
자동 실행(매시 정각)에는 도구 설정 '오늘의 추억 보이는 때'(꺼짐·매시·시각)에 맞을 때만 카드를 낸다.
"""
from __future__ import annotations

import datetime
import json
import random
from pathlib import Path

from common import MYDATA_DIR, TOOLS_DATA, T, load_config
from reporter import path_link
from scanner import get_photo_timestamp
from searcher import make_thumbnail

MEMORY_DIR = TOOLS_DATA / "memory"        # 오늘의 추억 상태(보인 사진 목록)와 카드용 큰 썸네일
SHOWN_FILE = MEMORY_DIR / "shown.json"
OFF_WORDS = {"", "off", "끄기", "꺼짐", "안 함", "none"}
HOURLY_WORDS = {"hourly", "매시", "매시간", "매시 정각"}


def is_due(memory_time: str, now: datetime.datetime) -> bool:
    """자동 실행이 지금 카드를 낼 때인가. 꺼짐이면 안 내고, 매시면 정각(5분 안), 'HH:MM'이면 그 시(정각 5분 안)."""
    value = str(memory_time or "").strip().lower()
    if now.minute >= 5 or value in OFF_WORDS:
        return False
    if value in HOURLY_WORDS:
        return True
    try:
        return now.hour == int(value.split(":")[0])
    except ValueError:
        return False


def parse_date(text: str | None) -> datetime.date | None:
    if not text or not isinstance(text, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y:%m:%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def collect_story_photos(analysis_files: list[Path] | None = None, roots: list[Path] | None = None) -> list[dict]:
    """모든 analysis.json 에서 이야기책 사진을 모은다. 찍은 시각이 없으면 원본 사진에서 읽어 analysis.json 에 채워 둔다."""
    if analysis_files is None:
        analysis_files = sorted(TOOLS_DATA.glob("*/analysis.json"))
    if roots is None:
        roots = [Path(r) for r in load_config().get("roots", []) if r]
    photos = []
    for af in analysis_files:
        try:
            data = json.loads(af.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder_name = data.get("folder", af.parent.name)
        folder_path = Path(data["folder_path"]) if data.get("folder_path") else None
        if not folder_path or not folder_path.exists():
            folder_path = next((root / folder_name for root in roots if (root / folder_name).exists()), None)
        modified = False
        for sc in data.get("scenes", []):
            story = sc.get("story") or {}
            story_text = story.get("text", "") or story.get("title", "")
            for an in sc.get("analyses", []):
                fn = an.get("filename")
                if not fn:
                    continue
                taken = an.get("taken")
                photo_file = (folder_path / fn) if folder_path else None
                if not taken and photo_file and photo_file.exists():
                    ts = get_photo_timestamp(photo_file)
                    if ts and ts != datetime.datetime.min:
                        taken = an["taken"] = ts.strftime("%Y-%m-%d %H:%M:%S")
                        modified = True
                photos.append({
                    "id": f"{folder_name}/{fn}", "folder": folder_name, "filename": fn,
                    "photo_path": photo_file or (af.parent / fn), "story_text": story_text,
                    "action_desc": an.get("action_and_mood", ""), "rotate": an.get("rotate", 0),
                    "taken_date": parse_date(taken),
                })
        if modified:
            try:
                af.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
    return photos


def choose_photo(photos: list[dict], shown_file: Path = SHOWN_FILE, today: datetime.date | None = None) -> dict | None:
    """오늘과 같은 월·일 사진을 먼저, 없으면 이야기책 사진 중 하나. 보인 사진은 한 바퀴 돌 때까지 다시 고르지 않는다."""
    if not photos:
        return None
    today = today or datetime.date.today()
    try:
        shown = json.loads(shown_file.read_text(encoding="utf-8"))
        shown = shown if isinstance(shown, list) else []
    except (OSError, ValueError):
        shown = []
    # 오늘과 같은 월·일, 지난해까지(올해 찍은 사진·찍은 시각을 못 읽어 파일 날짜가 오늘인 사진은 추억이 아니다)
    same_day = [p for p in photos if p["taken_date"] and p["taken_date"].year < today.year
                and (p["taken_date"].month, p["taken_date"].day) == (today.month, today.day)]
    pool = same_day or photos
    fresh = [p for p in pool if p["id"] not in set(shown)]
    if not fresh:   # 다 보였으면 이 묶음만 처음부터 다시
        ids = {p["id"] for p in pool}
        shown = [s for s in shown if s not in ids]
        fresh = list(pool)
    selected = random.choice(fresh)
    shown.append(selected["id"])
    shown_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        shown_file.write_text(json.dumps(shown, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return selected


def format_card(photo: dict, today: datetime.date | None = None) -> str:
    today = today or datetime.date.today()
    folder = photo["folder"]
    taken = photo.get("taken_date")
    # 카드 폭에 넓게 보여도 흐리지 않게 긴 쪽 1200px. 사진 찾기 썸네일(320px)과 섞이지 않게 따로 둔다.
    thumb = make_thumbnail(photo["photo_path"], folder, rotate=photo.get("rotate", 0), max_dim=1200, tools_data_dir=MEMORY_DIR)
    html_files = sorted(MYDATA_DIR.glob(f"*_{folder}.html"), reverse=True)
    html = html_files[0] if html_files else MYDATA_DIR / f"{folder}.html"
    when = ""
    if taken:
        day = taken.strftime("%Y-%m-%d")
        if (taken.month, taken.day) == (today.month, today.day):
            years = today.year - taken.year
            when = T("memory_years_ago", date=day, years=years) if years > 0 else T("memory_date", date=day)
        else:
            when = T("memory_date", date=day)
    desc = (photo.get("action_desc") or "").strip()
    lines = [T("memory_title", folder=folder, when=when), "", "> " + (desc or T("memory_default_summary")), ""]
    if Path(thumb).exists():
        lines += [f"![]({thumb})", ""]
    story = (photo.get("story_text") or "").strip()
    if story:
        lines += [story, ""]
    lines.append(T("memory_links", html=path_link(html), photo=path_link(photo["photo_path"])))
    return "\n".join(lines).strip() + "\n"


def show(trigger: str = "quick", now: datetime.datetime | None = None) -> str:
    """카드 마크다운. 자동 실행은 때가 아니거나 사진이 없으면 빈 글(알리지 않음)."""
    now = now or datetime.datetime.now()
    scheduled = trigger == "schedule"
    if scheduled and not is_due(load_config().get("memory_time", "09:00"), now):
        return ""
    selected = choose_photo(collect_story_photos(), SHOWN_FILE, now.date())
    if not selected:
        return "" if scheduled else T("memory_none")
    return format_card(selected, now.date())
