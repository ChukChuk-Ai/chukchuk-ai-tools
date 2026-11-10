"""사진 메타데이터 스캔, 시간순 정렬, 장면 나누기."""
from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
from PIL import Image, ExifTags

from common import T

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
SKIPPED_EXTENSIONS = {".heic", ".heif", ".mp4", ".mov", ".avi", ".mkv", ".wmv"}


def parse_exif_datetime(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    # YYYY:MM:DD HH:MM:SS or YYYY-MM-DD HH:MM:SS
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            pass
    return None


def get_photo_timestamp(path: Path) -> datetime:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                # 36867 = DateTimeOriginal, 306 = DateTime
                dto = exif.get(36867) or exif.get(0x9003)
                if not dto and hasattr(ExifTags, "TAGS"):
                    for k, v in ExifTags.TAGS.items():
                        if v == "DateTimeOriginal":
                            dto = exif.get(k)
                            break
                parsed = parse_exif_datetime(dto)
                if parsed:
                    return parsed
                # fallback to DateTime (306)
                dt = exif.get(306)
                parsed = parse_exif_datetime(dt)
                if parsed:
                    return parsed
    except Exception:
        pass

    try:
        mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime)
    except Exception:
        return datetime.min


def scan_folder(folder_path: Path) -> tuple[list[dict], int]:
    """폴더 내 사진 스캔 및 시간순 정렬.

    Returns:
        (사진_항목_목록, 건너뛴_미디어_수)
        사진 항목: {
            "path": Path,
            "filename": str,
            "timestamp": datetime,
            "time_str": str,
        }
    """
    photos: list[dict] = []
    skipped_count = 0

    try:
        for entry in folder_path.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                ts = get_photo_timestamp(entry)
                photos.append({
                    "path": entry,
                    "filename": entry.name,
                    "timestamp": ts,
                    "time_str": ts.strftime("%Y-%m-%d %H:%M:%S") if ts != datetime.min else T("unknown"),
                })
            elif ext in SKIPPED_EXTENSIONS:
                skipped_count += 1
    except Exception as e:
        raise RuntimeError(T("folder_read_failed", err=e))

    # 시간순 정렬 (시각 같으면 파일 이름 오름차순)
    photos.sort(key=lambda x: (x["timestamp"], x["filename"].lower()))
    return photos, skipped_count


def group_scenes(photos: list[dict], scene_gap_minutes: int = 30) -> list[dict]:
    """시간 간격(기본 30분) 기준으로 사진들을 장면으로 나눈다.

    Returns:
        [
            {
                "scene_idx": 1,
                "start_time": datetime,
                "end_time": datetime,
                "start_time_str": str,
                "end_time_str": str,
                "photos": [사진항목, ...],
            },
            ...
        ]
    """
    if not photos:
        return []

    scenes: list[dict] = []
    current_scene_photos: list[dict] = []
    gap = timedelta(minutes=scene_gap_minutes)

    for p in photos:
        if not current_scene_photos:
            current_scene_photos.append(p)
            continue

        prev_ts = current_scene_photos[-1]["timestamp"]
        curr_ts = p["timestamp"]

        # 시간 차이가 gap을 넘으면 새 장면 시작
        if curr_ts - prev_ts > gap:
            scene_idx = len(scenes) + 1
            s_time = current_scene_photos[0]["timestamp"]
            e_time = current_scene_photos[-1]["timestamp"]
            scenes.append({
                "scene_idx": scene_idx,
                "start_time": s_time,
                "end_time": e_time,
                "start_time_str": s_time.strftime("%H:%M") if s_time != datetime.min else "00:00",
                "end_time_str": e_time.strftime("%H:%M") if e_time != datetime.min else "00:00",
                "photos": current_scene_photos,
            })
            current_scene_photos = [p]
        else:
            current_scene_photos.append(p)

    if current_scene_photos:
        scene_idx = len(scenes) + 1
        s_time = current_scene_photos[0]["timestamp"]
        e_time = current_scene_photos[-1]["timestamp"]
        scenes.append({
            "scene_idx": scene_idx,
            "start_time": s_time,
            "end_time": e_time,
            "start_time_str": s_time.strftime("%H:%M") if s_time != datetime.min else "00:00",
            "end_time_str": e_time.strftime("%H:%M") if e_time != datetime.min else "00:00",
            "photos": current_scene_photos,
        })

    return scenes
