"""썸네일 판(그리드 시트) 생성 모듈."""
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps


def get_short_label(filename: str) -> str:
    """파일명에서 짧은 라벨 추출 (예: DSC00410.JPG -> 0410, IMG_1234 -> 1234)."""
    stem = Path(filename).stem
    # 숫자가 포함되어 있으면 마지막 연속 숫자 위주로, 아니면 뒤의 6~8자
    digits = ""
    for ch in reversed(stem):
        if ch.isdigit():
            digits = ch + digits
        elif digits:
            break
    if digits and len(digits) >= 3:
        return digits[-6:]
    return stem[-8:] if len(stem) > 8 else stem


def create_sheet_image(
    photos: list[dict],
    cols: int = 8,
    rows: int = 5,
    max_dimension: int = 1568,
) -> tuple[Image.Image, list[dict]]:
    """사진 목록으로 그리드 썸네일 시트를 생성한다.

    Returns:
        (PIL.Image, 사진_라벨_정보_목록)
    """
    cell_w = max_dimension // cols  # 예: 1568 // 8 = 196
    text_h = 24
    img_box_h = cell_w - text_h    # 예: 196 - 24 = 172
    cell_h = img_box_h + text_h    # 196

    sheet_w = cols * cell_w
    sheet_h = rows * cell_h

    sheet = Image.new("RGB", (sheet_w, sheet_h), color=(240, 240, 240))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    items: list[dict] = []

    for idx, p in enumerate(photos):
        if idx >= cols * rows:
            break

        c = idx % cols
        r = idx // cols

        x0 = c * cell_w
        y0 = r * cell_h

        label = get_short_label(p["filename"])
        items.append({
            "index": idx + 1,
            "label": label,
            "filename": p["filename"],
            "path": p["path"],
            "photo": p,
        })

        # 칸 배경 및 테두리
        draw.rectangle([x0 + 1, y0 + 1, x0 + cell_w - 2, y0 + cell_h - 2], fill=(255, 255, 255), outline=(200, 200, 200))

        # 이미지 열어서 EXIF 방향 보정 및 썸네일 생성
        try:
            with Image.open(p["path"]) as orig:
                transposed = ImageOps.exif_transpose(orig)
                transposed.thumbnail((cell_w - 6, img_box_h - 6), Image.Resampling.LANCZOS)

                # 중앙 정렬 배치
                tw, th = transposed.size
                tx = x0 + (cell_w - tw) // 2
                ty = y0 + (img_box_h - th) // 2
                sheet.paste(transposed, (tx, ty))
        except Exception:
            # 읽기 실패 시 회색 박스
            draw.rectangle([x0 + 4, y0 + 4, x0 + cell_w - 4, y0 + img_box_h - 4], fill=(220, 220, 220))
            draw.text((x0 + 10, y0 + img_box_h // 2 - 6), "ERR", fill=(150, 0, 0), font=font)

        # 라벨 및 인덱스 표시
        label_text = f"#{idx+1} {label}"
        # 텍스트 중앙 계산
        bbox = draw.textbbox((0, 0), label_text, font=font)
        text_w = bbox[2] - bbox[0]
        tx = x0 + max(2, (cell_w - text_w) // 2)
        ty = y0 + img_box_h + 4
        draw.text((tx, ty), label_text, fill=(30, 30, 30), font=font)

    return sheet, items


def make_thumbnail_sheets(
    photos: list[dict],
    out_dir: Path,
    sheet_size: int = 40,
    cols: int = 8,
    rows: int = 5,
    max_dimension: int = 1568,
    prefix: str = "sheet",
) -> list[dict]:
    """사진 목록을 sheet_size(기본 40) 장씩 나누어 썸네일 판들을 생성하고 out_dir에 저장한다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets_info: list[dict] = []

    total = len(photos)
    num_sheets = math.ceil(total / sheet_size) if total > 0 else 0

    for s_idx in range(num_sheets):
        start = s_idx * sheet_size
        end = min(start + sheet_size, total)
        chunk = photos[start:end]

        sheet_img, items = create_sheet_image(chunk, cols=cols, rows=rows, max_dimension=max_dimension)
        sheet_path = out_dir / f"{prefix}_{s_idx+1:02d}.jpg"
        sheet_img.save(sheet_path, "JPEG", quality=85)

        sheets_info.append({
            "sheet_idx": s_idx + 1,
            "sheet_path": sheet_path,
            "items": items,
            "count": len(items),
        })

    return sheets_info
