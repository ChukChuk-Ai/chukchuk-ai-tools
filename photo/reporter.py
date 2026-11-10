"""HTML 이야기책과 완료 카드 글 만들기(카드를 남기고 텔레그램으로 보내는 일은 앱이 한다)."""
from __future__ import annotations

import base64
from datetime import datetime
import io
from pathlib import Path
from PIL import Image, ImageOps

from common import MYDATA_DIR, T


def apply_rotation(img: Image.Image, rotate: int) -> Image.Image:
    """시계방향(clockwise) rotate 도(0, 90, 180, 270)만큼 회전."""
    deg = rotate % 360
    if deg == 90:
        return img.transpose(Image.Transpose.ROTATE_270)
    elif deg == 180:
        return img.transpose(Image.Transpose.ROTATE_180)
    elif deg == 270:
        return img.transpose(Image.Transpose.ROTATE_90)
    return img


def encode_image_for_html(path: Path, max_dimension: int = 1024, quality: int = 80, rotate: int = 0) -> str:
    """HTML 인라인 삽입을 위해 긴 쪽을 1024px로 줄인 base64 JPEG 문자열 생성."""
    try:
        with Image.open(path) as img:
            transposed = ImageOps.exif_transpose(img)
            if rotate % 360 != 0:
                transposed = apply_rotation(transposed, rotate)
            w, h = transposed.size
            if max(w, h) > max_dimension:
                scale = max_dimension / max(w, h)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                transposed = transposed.resize((new_w, new_h), Image.Resampling.LANCZOS)
            if transposed.mode != "RGB":
                transposed = transposed.convert("RGB")
            buffer = io.BytesIO()
            transposed.save(buffer, format="JPEG", quality=quality)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception:
        return ""


def build_story_html(
    folder_name: str,
    folder_path: Path,
    scenes: list[dict],
    selected_photos: list[dict],
    photo_analyses_by_scene: dict[int, list[dict]],
    stories: dict[int, dict],
    people: list[dict],
    total_photos: int,
    readable_count: int,
    usage_by_model: dict[str, dict[str, int]],
) -> Path:
    """단일 HTML 이야기책 파일을 생성하고 저장한다."""
    MYDATA_DIR.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now().strftime("%Y%m%d-%H%M")
    html_path = MYDATA_DIR / f"{now_str}_{folder_name}.html"

    # 장면별 렌더링 HTML 조각 생성
    scenes_html_blocks = []
    for sc in scenes:
        idx = sc["scene_idx"]
        st = stories.get(idx, {"title": T("scene_n", n=idx), "text": ""})
        pa_list = photo_analyses_by_scene.get(idx, [])
        pa_map = {p.get("filename"): p for p in pa_list}

        # 이 장면에 속한 고른 사진들
        sc_photos = [p for p in selected_photos if any(sp["filename"] == p["filename"] for sp in sc["photos"])]

        photo_cards = []
        for p in sc_photos:
            data_uri = encode_image_for_html(p["path"], rotate=p.get("rotate", 0))
            pa = pa_map.get(p["filename"], {})
            action_text = pa.get("action_and_mood", "")
            peeps = ", ".join(pa.get("people_present", []))
            caption = f"<div class='caption'><strong>{p['filename']}</strong> ({p['time_str']})"
            if peeps:
                caption += f"<br><span class='people-tag'>{T('html_people_tag', people=peeps)}</span>"
            if action_text:
                caption += f"<p class='action-desc'>{action_text}</p>"
            caption += "</div>"

            photo_cards.append(f"""
            <div class="photo-card">
              <img src="{data_uri}" alt="{p['filename']}" loading="lazy"/>
              {caption}
            </div>
            """)

        photos_grid = "\n".join(photo_cards) if photo_cards else f"<p class='no-photo'>{T('html_no_photo')}</p>"

        scenes_html_blocks.append(f"""
        <section class="scene-section">
          <div class="scene-header">
            <span class="scene-badge">{T("scene_n", n=idx)}</span>
            <span class="scene-time">{sc['start_time_str']} ~ {sc['end_time_str']}</span>
            <h2>{st['title']}</h2>
          </div>
          <div class="story-box">
            <p>{st['text']}</p>
          </div>
          <div class="photo-grid">
            {photos_grid}
          </div>
        </section>
        """)

    # 등장인물 표 HTML
    people_rows = []
    for pe in people:
        people_rows.append(f"""
        <tr>
          <td><strong>{pe.get('name', '')}</strong></td>
          <td>{pe.get('features', '')}</td>
          <td>{pe.get('first_seen', '')}</td>
        </tr>
        """)
    people_table_html = "\n".join(people_rows) if people_rows else f"<tr><td colspan='3'>{T('html_no_people')}</td></tr>"

    html_content = f"""<!DOCTYPE html>
<html lang="{T("html_lang")}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{T("html_title", name=folder_name)}</title>
<style>
  :root {{
    --bg-color: #f8fafc;
    --card-bg: #ffffff;
    --primary: #2563eb;
    --primary-light: #eff6ff;
    --text-main: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
    background-color: var(--bg-color);
    color: var(--text-main);
    line-height: 1.6;
    margin: 0;
    padding: 20px;
  }}
  .container {{
    max-width: 1000px;
    margin: 0 auto;
    background: var(--card-bg);
    border-radius: 16px;
    padding: 36px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
  }}
  header {{
    text-align: center;
    border-bottom: 2px solid var(--border);
    padding-bottom: 24px;
    margin-bottom: 32px;
  }}
  header h1 {{
    font-size: 2.2rem;
    margin: 0 0 8px 0;
    color: #0f172a;
  }}
  .subtitle {{
    font-size: 1.05rem;
    color: var(--text-muted);
  }}
  .people-section {{
    margin-bottom: 40px;
    background: var(--primary-light);
    padding: 20px 24px;
    border-radius: 12px;
  }}
  .people-section h3 {{
    margin-top: 0;
    margin-bottom: 12px;
    color: var(--primary);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border-radius: 8px;
    overflow: hidden;
  }}
  th, td {{
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    font-size: 0.95rem;
  }}
  th {{
    background-color: #f1f5f9;
    color: #475569;
  }}
  .scene-section {{
    margin-bottom: 48px;
    padding-bottom: 32px;
    border-bottom: 1px dashed var(--border);
  }}
  .scene-header {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }}
  .scene-badge {{
    background: var(--primary);
    color: #fff;
    padding: 4px 10px;
    border-radius: 20px;
    font-weight: bold;
    font-size: 0.85rem;
  }}
  .scene-time {{
    font-size: 0.95rem;
    color: var(--text-muted);
    font-weight: 500;
  }}
  .scene-header h2 {{
    margin: 0;
    font-size: 1.4rem;
    color: #0f172a;
  }}
  .story-box {{
    background: #fdfaf6;
    border-left: 4px solid #f59e0b;
    padding: 14px 18px;
    border-radius: 0 8px 8px 0;
    font-size: 1.05rem;
    margin-bottom: 20px;
    color: #334155;
  }}
  .photo-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 20px;
  }}
  .photo-card {{
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 2px 6px rgba(0,0,0,0.03);
  }}
  .photo-card img {{
    width: 100%;
    height: 220px;
    object-fit: cover;
    display: block;
  }}
  .caption {{
    padding: 12px;
    font-size: 0.88rem;
  }}
  .people-tag {{
    color: var(--primary);
    font-size: 0.82rem;
  }}
  .action-desc {{
    margin: 6px 0 0 0;
    color: #475569;
  }}
  footer {{
    margin-top: 48px;
    padding-top: 20px;
    border-top: 2px solid var(--border);
    font-size: 0.85rem;
    color: var(--text-muted);
    text-align: right;
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>{T("html_title", name=folder_name)}</h1>
    <div class="subtitle">{folder_path} · {T("html_shot_date", date=scenes[0]['photos'][0]['timestamp'].strftime('%Y-%m-%d') if scenes and scenes[0]['photos'] else '')}</div>
  </header>

  <section class="people-section">
    <h3>{T("html_people_heading")}</h3>
    <table>
      <thead>
        <tr>
          <th>{T("html_th_name")}</th>
          <th>{T("html_th_features")}</th>
          <th>{T("html_th_first")}</th>
        </tr>
      </thead>
      <tbody>
        {people_table_html}
      </tbody>
    </table>
  </section>

  {"".join(scenes_html_blocks)}

  <footer>
    <p>{T("html_footer_counts", total=total_photos, readable=readable_count, selected=len(selected_photos))}</p>
    <p>{T("html_footer_time", time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
  </footer>
</div>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return html_path




def path_link(path) -> str:
    """화이트보드용 경로 링크: 보이는 글은 경로 그대로, 누르면 파일은 기본 프로그램·폴더는 탐색기로 열린다.
    빈칸·한글은 주소에서 바꾸고, 보이는 글의 역슬래시는 마크다운에서 줄지 않게 두 번 쓴다."""
    from urllib.parse import quote
    text = str(path)
    posix = text.replace("\\", "/")
    url = ("file:" + quote(posix, safe="/:")) if posix.startswith("//") else ("file:///" + quote(posix, safe="/:"))
    return f"[{text.replace(chr(92), chr(92) * 2)}]({url})"

def format_finish_card(
    folder_name: str,
    folder_path: Path,
    total_count: int,
    readable_count: int,
    selected_count: int,
    scenes: list[dict],
    stories: dict[int, dict],
    photo_analyses_by_scene: dict[int, list[dict]],
    people: list[dict],
    html_path: Path,
) -> str:
    """결과 양식(README "도구 만드는 법")에 맞춘 완료 카드 마크다운."""
    people_count = len(people)
    scenes_count = len(scenes)

    # 요약 줄: [분류] [출처] 핵심 내용 (범위·조건)
    summary_line = T("card_summary", link=path_link(folder_path), total=total_count, readable=readable_count, selected=selected_count,
                     scenes=scenes_count, people=people_count)

    # 표: 장면
    scene_rows = []
    for sc in scenes:
        idx = sc["scene_idx"]
        st = stories.get(idx, {"title": T("scene_n", n=idx)})
        pa = photo_analyses_by_scene.get(idx, [])
        all_p = set()
        for p in pa:
            for pe in p.get("people_present", []):
                all_p.add(pe)
        p_str = ", ".join(sorted(all_p)) if all_p else "-"
        s_time = f"{sc['start_time_str']}~{sc['end_time_str']}"
        scene_rows.append(T("card_scene_row", n=idx, time=s_time, title=st['title'], people=p_str, count=len(sc['photos'])))

    scene_table = "\n".join([
        T("card_scene_head"),
        *scene_rows,
    ])

    # 등장인물 표
    people_rows = []
    for pe in people:
        people_rows.append(f"| {pe.get('name', '')} | {pe.get('features', '')} |")
    people_table = "\n".join([
        T("card_people_head"),
        *people_rows,
    ])

    # 사용자 요청(2026-11-08): 이야기책 바로보기 링크와 사진 정보를 맨 위에
    card_text = T("card_story", name=folder_name, link=path_link(html_path.resolve()), summary=summary_line,
                  scenes=scene_table, people=people_table)
    return card_text


