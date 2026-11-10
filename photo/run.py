"""사진 도구 메인 실행 진입점."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))   # 같은 폴더의 모듈(common·worker …). sdk 는 common 이 찾는다

from ai import PhotoAnalyzer, apply_name_mapping, rewrite_analysis_texts
from common import MYDATA_DIR, TOOLS_DATA, T, load_config
from sdk import system
from finder import find_photo_folder
from reporter import build_story_html, format_finish_card, path_link
from scanner import get_photo_timestamp
from searcher import format_search_card, search_photos
import waiting
from worker import LOCK_FILE, STATUS_FILE, is_process_running, load_people, protected_people, save_people  # noqa: E402

OUT_LOG = TOOLS_DATA / "runner_out.log"
WORKER_SCRIPT = HERE / "worker.py"
VIDEO_SCRIPT = HERE / "video.py"
GROWTH_SCRIPT = HERE / "growth.py"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}



def spawn_worker(folder_path: Path, script: Path | str | None = None) -> None:
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(OUT_LOG, "a", encoding="utf-8") as log:
        log.write(T("log_worker_start", time=now_str, path=folder_path))
    system.spawn_background([sys.executable, str(script or WORKER_SCRIPT), str(folder_path)], OUT_LOG, cwd=str(HERE))


def running_lock() -> dict | None:
    """작업이 실제로 돌고 있으면 잠금 정보, 아니면 None(죽은 프로세스의 잠금은 치운다)."""
    if not LOCK_FILE.exists():
        return None
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if is_process_running(info.get("pid", 0)):
            return info
        LOCK_FILE.unlink()
    except Exception:
        pass
    return None


def print_queue_table(items: list[dict]) -> None:
    if not items:
        return
    print(T("queue_head"))
    for i, it in enumerate(items, 1):
        print(T("queue_row", i=i, name=it.get('name', ''), photos=it.get('photos', 0)))
    print()


def handle_queue_clear() -> None:
    n = waiting.clear_queue()
    print(T("queue_clear_title"))
    print(T("queue_cleared", n=n) if n else T("queue_already_empty"))


def handle_story_many(names: list[str], fresh: bool = False) -> None:
    """쉼표 등으로 여러 폴더를 시켰을 때: 찾아서 대기 줄에 올리고, 일이 없으면 첫 폴더부터 시작한다. 고르기 창은 띄우지 않는다."""
    roots = load_config().get("roots", [])
    skipped, added = [], []   # added: (이름, 경로, 사진 수)
    for name in names:
        matched, candidates = find_photo_folder(name, roots)
        if candidates:
            skipped.append(T("skip_similar", name=name))
            continue
        if not matched:
            skipped.append(T("skip_not_found", name=name))
            continue
        if not fresh and (TOOLS_DATA / matched.name / "analysis.json").exists():
            skipped.append(T("skip_exists", name=matched.name))
            continue
        if any(a[1] == matched for a in added):
            continue
        try:
            count = sum(1 for f in matched.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS)
        except Exception:
            count = 0
        if not count:
            skipped.append(T("skip_no_photos", name=matched.name))
            continue
        added.append((matched.name, matched, count))

    busy = running_lock()
    for _, path, count in added:
        waiting.enqueue(path, count)
    started = None
    if added and not busy:
        started = waiting.pop_next()
        if started:
            spawn_worker(Path(started["folder_path"]))
    queue_now = waiting.load_queue()

    if not added:
        print(T("many_none"))
    elif started:
        rest = len(added) - 1
        print(T("story_start_title", name=added[0][0]) + (T("and_more", n=rest) + "\n" if rest else "\n"))
    else:
        cur = busy.get("folder", "") if busy else ""
        print(T("story_queue_title", name=added[0][0]) + (T("and_more", n=len(added) - 1) + "\n" if len(added) > 1 else "\n"))
        print(T("many_queued_note", cur=cur))
    if added:
        print(T("many_head"))
        for i, (name, path, count) in enumerate(added, 1):
            if started and i == 1:
                state = T("state_now")
            else:
                pos = next((k for k, it in enumerate(queue_now, 1) if it.get("folder_path") == str(path)), 0)
                state = T("state_queued", pos=pos)
            print(T("many_row", i=i, name=name, count=count, state=state))
        print()
        print(T("many_footer"))
    for line in skipped:
        print(f"\n> {line}")


def handle_status() -> None:
    is_running = False
    lock_info = {}
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                lock_info = json.load(f)
            pid = lock_info.get("pid", 0)
            if is_process_running(pid):
                is_running = True
            else:
                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    status_info = {}
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                status_info = json.load(f)
        except Exception:
            pass

    if is_running:
        folder = lock_info.get("folder", status_info.get("folder", T("unknown")))
        step_name = status_info.get("step_name", T("status_preparing"))
        progress = status_info.get("progress", "")
        started_at = lock_info.get("started_at", status_info.get("started_at", ""))
        step = status_info.get("step", 1)
        total_steps = status_info.get("total_steps", 6)

        print(T("status_running_title", folder=folder))
        print(T("status_running_line", folder=folder, step=step, total=total_steps, step_name=step_name, progress=progress,
                started=started_at))
        print(T("status_head"))
        print(T("status_row", folder=folder, step=step, total=total_steps, step_name=step_name, progress=progress, started=started_at))
        queue_items = waiting.load_queue()
        if queue_items:
            print(T("queue_count", n=len(queue_items)))
            print_queue_table(queue_items)
        print(T("status_running_footer"))
    else:
        last_folder = status_info.get("folder", "")
        last_status = status_info.get("status", "")
        last_updated = status_info.get("updated_at", "")

        print(T("status_idle_title"))
        print(T("status_idle"))
        if last_folder:
            print(T("status_last", folder=last_folder, status=last_status, updated=last_updated))
        queue_items = waiting.load_queue()
        if queue_items:
            print(T("queue_count_idle", n=len(queue_items)))
            print_queue_table(queue_items)
        print(T("status_idle_footer"))


def handle_cached_story(folder_name: str, folder_path: Path, analysis_data: dict) -> None:
    """이미 분석된 폴더에 대해 AI 호출 없이 기존 결과로 카드 출력."""
    total_count = analysis_data.get("total_photos", 0)
    readable_count = analysis_data.get("readable_photos", 0)
    selected_count = len(analysis_data.get("selected_photos", []))
    people = analysis_data.get("people", [])

    html_files = sorted(MYDATA_DIR.glob(f"*_{folder_name}.html"), reverse=True)
    html_path = html_files[0] if html_files else (MYDATA_DIR / f"{folder_name}.html")

    scenes_data = analysis_data.get("scenes", [])
    reconstructed_scenes = []
    stories = {}
    photo_analyses_by_scene = {}

    for sc in scenes_data:
        idx = sc.get("scene_idx", 1)
        reconstructed_scenes.append({
            "scene_idx": idx,
            "start_time_str": sc.get("start_time", "00:00"),
            "end_time_str": sc.get("end_time", "00:00"),
            "photos": [{} for _ in range(sc.get("photos_count", 0))],
        })
        stories[idx] = sc.get("story", {"title": T("scene_n", n=idx)})
        photo_analyses_by_scene[idx] = sc.get("analyses", [])

    card_text = format_finish_card(
        folder_name=folder_name,
        folder_path=folder_path,
        total_count=total_count,
        readable_count=readable_count,
        selected_count=selected_count,
        scenes=reconstructed_scenes,
        stories=stories,
        photo_analyses_by_scene=photo_analyses_by_scene,
        people=people,
        html_path=html_path,
    )
    print(card_text)


def handle_rename(folder_name: str, from_name: str, to_name: str) -> None:
    cfg = load_config()
    folder_data_dir = TOOLS_DATA / folder_name
    analysis_file = folder_data_dir / "analysis.json"
    if not analysis_file.exists():
        sys.stderr.write(T("no_analysis", name=folder_name) + "\n")
        sys.exit(1)

    with open(analysis_file, "r", encoding="utf-8") as f:
        analysis_data = json.load(f)

    # rename된 이름은 보존 목록에 추가
    renamed_list = analysis_data.setdefault("renamed_people", [])
    if to_name not in renamed_list:
        renamed_list.append(to_name)
    if from_name in renamed_list:
        renamed_list.remove(from_name)

    people = load_people()
    for p in people:
        if p.get("name") == from_name:
            p["name"] = to_name
            p["manual"] = True
    save_people(people)

    for p in analysis_data.get("people", []):
        if p.get("name") == from_name:
            p["name"] = to_name
            p["manual"] = True

    for sc in analysis_data.get("scenes", []):
        for an in sc.get("analyses", []):
            an["people_present"] = [to_name if x == from_name else x for x in an.get("people_present", [])]
    # 사진별 설명 글·인물 특징 속의 옛 이름도 함께 바꾼다(AI 없음)
    rewrite_analysis_texts(analysis_data, {from_name: to_name})

    from ai import PhotoAnalyzer
    analyzer = PhotoAnalyzer(cfg.get("sort_model", "cli-claude-haiku-5-5"), cfg.get("story_model", "cli-claude-sonnet-5-5"))

    scenes_input = []
    pa_map = {}
    for sc in analysis_data.get("scenes", []):
        idx = sc.get("scene_idx", 1)
        scenes_input.append({
            "scene_idx": idx,
            "start_time_str": sc.get("start_time", "00:00"),
            "end_time_str": sc.get("end_time", "00:00"),
            "photos": [{} for _ in range(sc.get("photos_count", 0))],
        })
        pa_map[idx] = sc.get("analyses", [])

    new_stories = analyzer.write_story_texts(
        folder_name=folder_name,
        scenes=scenes_input,
        photo_analyses_by_scene=pa_map,
        people=analysis_data.get("people", []),
    )

    for sc in analysis_data.get("scenes", []):
        idx = sc.get("scene_idx", 1)
        if idx in new_stories:
            sc["story"] = new_stories[idx]

    usage = analysis_data.setdefault("usage", {})
    for m, u in analyzer.usage.items():
        t = usage.setdefault(m, {"input_tokens": 0, "output_tokens": 0})
        t["input_tokens"] = t.get("input_tokens", 0) + u.get("input_tokens", 0)
        t["output_tokens"] = t.get("output_tokens", 0) + u.get("output_tokens", 0)

    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(analysis_data, f, ensure_ascii=False, indent=2)

    # 이름이 바뀐 이야기책(HTML)과 카드를 새로 만든다(사진 AI 호출 없음)
    folder_path = Path(analysis_data.get("folder_path", ""))
    publish_story(folder_name, folder_path, analysis_data, cfg)


def publish_story(folder_name: str, folder_path: Path, analysis_data: dict, cfg: dict) -> None:
    """저장된 분석(analysis_data)으로 HTML 이야기책과 화이트보드 카드를 새로 만든다(AI 호출 없음).
    인물 정리(regroup)와 이름 고치기(rename)가 함께 쓴다."""
    scenes_data = analysis_data.get("scenes", [])
    updated_pa_map = {sc.get("scene_idx", 1): sc.get("analyses", []) for sc in scenes_data}
    new_stories = {sc.get("scene_idx", 1): sc["story"] for sc in scenes_data if sc.get("story")}
    final_people = analysis_data.get("people", [])
    total_usage = analysis_data.get("usage", {})
    # 7. HTML 이야기책 재생성
    selected_objs = []
    photos_on_disk = {}
    if folder_path.exists():
        try:
            photos_on_disk = {f.name: f for f in folder_path.iterdir() if f.is_file()}
        except Exception:
            pass

    rotations = analysis_data.get("rotations", {})
    for fn in analysis_data.get("selected_photos", []):
        if fn in photos_on_disk:
            p_file = photos_on_disk[fn]
            ts = get_photo_timestamp(p_file)
            selected_objs.append({
                "path": p_file,
                "filename": fn,
                "timestamp": ts,
                "time_str": ts.strftime("%H:%M") if ts != datetime.datetime.min else "",
                "rotate": rotations.get(fn, 0),
            })

    scenes_for_html = []
    for sc in scenes_data:
        idx = sc.get("scene_idx", 1)
        sc_analyses = sc.get("analyses", [])
        sc_filenames = {a.get("filename") for a in sc_analyses}
        sc_photos = [p for p in selected_objs if p["filename"] in sc_filenames]
        if not sc_photos:
            sc_photos = list(selected_objs)
        scenes_for_html.append({
            "scene_idx": idx,
            "start_time_str": sc.get("start_time", "00:00"),
            "end_time_str": sc.get("end_time", "00:00"),
            "photos": sc_photos,
        })

    html_path = build_story_html(
        folder_name=folder_name,
        folder_path=folder_path,
        scenes=scenes_for_html,
        selected_photos=selected_objs,
        photo_analyses_by_scene=updated_pa_map,
        stories=new_stories,
        people=final_people,
        total_photos=analysis_data.get("total_photos", len(selected_objs)),
        readable_count=analysis_data.get("readable_photos", len(selected_objs)),
        usage_by_model=total_usage,
    )

    # 8. 화이트보드 카드 작성
    card_content = format_finish_card(
        folder_name=folder_name,
        folder_path=folder_path,
        total_count=analysis_data.get("total_photos", len(selected_objs)),
        readable_count=analysis_data.get("readable_photos", len(selected_objs)),
        selected_count=len(selected_objs),
        scenes=scenes_for_html,
        stories=new_stories,
        photo_analyses_by_scene=updated_pa_map,
        people=final_people,
        html_path=html_path,
    )

    print(card_content)   # 앱이 결과 카드로 남긴다


def handle_rotate(folder_query: str) -> None:
    """'회전 판단 + 이야기책·카드 다시 만들기':
    기존 analysis.json의 고른 사진(selected_photos)만 썸네일 판으로 묶어
    Haiku에게 회전 방향(0, 90, 180, 270)을 묻고, 결과를 저장한 뒤 HTML과 카드를 재생성한다."""
    cfg = load_config()
    roots = cfg.get("roots", [])

    matched_path, candidates = find_photo_folder(folder_query, roots) if folder_query else (None, [])
    if candidates or not matched_path:
        sys.stderr.write(T("folder_not_found", query=folder_query) + "\n")
        sys.exit(1)

    folder_name = matched_path.name
    folder_data_dir = TOOLS_DATA / folder_name
    analysis_file = folder_data_dir / "analysis.json"
    if not analysis_file.exists():
        sys.stderr.write(T("no_analysis", name=folder_name) + "\n")
        sys.exit(1)

    with open(analysis_file, "r", encoding="utf-8") as f:
        analysis_data = json.load(f)

    analyzer = PhotoAnalyzer(
        sort_model=cfg.get("sort_model", "cli-claude-haiku-5-5"),
        story_model=cfg.get("story_model", "cli-claude-sonnet-5-5"),
    )

    photos_on_disk = {}
    if matched_path.exists():
        try:
            photos_on_disk = {f.name: f for f in matched_path.iterdir() if f.is_file()}
        except Exception:
            pass

    selected_objs = []
    for fn in analysis_data.get("selected_photos", []):
        if fn in photos_on_disk:
            p_file = photos_on_disk[fn]
            ts = get_photo_timestamp(p_file)
            selected_objs.append({
                "path": p_file,
                "filename": fn,
                "timestamp": ts,
                "time_str": ts.strftime("%H:%M") if ts != datetime.datetime.min else "",
            })

    if not selected_objs:
        sys.stderr.write(T("no_selected", name=folder_name) + "\n")
        sys.exit(1)

    rotate_enabled = bool(cfg.get("rotate", True))
    if rotate_enabled:
        sheets_dir = folder_data_dir / "sheets"
        rotations = analyzer.detect_rotations(selected_objs, sheets_dir=sheets_dir)
    else:
        rotations = {}

    analysis_data["rotations"] = rotations
    all_classification = analysis_data.setdefault("classification", {})
    for fn, r_val in rotations.items():
        if fn in all_classification:
            all_classification[fn]["rotate"] = r_val
        else:
            all_classification[fn] = {"rotate": r_val}

    for sc in analysis_data.get("scenes", []):
        for an in sc.get("analyses", []):
            an["rotate"] = rotations.get(an.get("filename"), 0)

    prev_usage = analysis_data.get("usage", {})
    total_usage = {}
    for m in set(list(prev_usage.keys()) + list(analyzer.usage.keys())):
        pu = prev_usage.get(m, {"input_tokens": 0, "output_tokens": 0})
        nu = analyzer.usage.get(m, {"input_tokens": 0, "output_tokens": 0})
        total_usage[m] = {
            "input_tokens": pu.get("input_tokens", 0) + nu.get("input_tokens", 0),
            "output_tokens": pu.get("output_tokens", 0) + nu.get("output_tokens", 0),
        }
    analysis_data["usage"] = total_usage
    analysis_data["rotate_usage"] = analyzer.usage

    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(analysis_data, f, ensure_ascii=False, indent=2)

    publish_story(folder_name, matched_path, analysis_data, cfg)


def handle_regroup_story(folder_name: str, folder_path: Path, analysis_data: dict) -> None:
    """기존 분석 데이터를 바탕으로 사진 AI 호출 없이 인물 정리 + 이야기 쓰기만 다시 수행한다."""
    cfg = load_config()
    analyzer = PhotoAnalyzer(
        sort_model=cfg.get("sort_model", "cli-claude-haiku-5-5"),
        story_model=cfg.get("story_model", "cli-claude-sonnet-5-5"),
    )

    # 1. 보호할 이름 (사용자가 수정한 이름)
    current_people = analysis_data.get("people", [])
    preserved_names, preserved_people = protected_people(analysis_data.get("renamed_people", []), current_people)

    # 2. 장면 및 사진별 분석 정보 구성
    scenes_data = analysis_data.get("scenes", [])
    scenes_input = []
    pa_map = {}
    for sc in scenes_data:
        idx = sc.get("scene_idx", 1)
        scenes_input.append({
            "scene_idx": idx,
            "start_time_str": sc.get("start_time", "00:00"),
            "end_time_str": sc.get("end_time", "00:00"),
            "photos": [{} for _ in range(sc.get("photos_count", 0))],
        })
        pa_map[idx] = sc.get("analyses", [])

    # 3. 인물 정리 (Sonnet 글만 1회 호출)
    name_mapping, regrouped_people = analyzer.regroup_people(
        folder_name=folder_name,
        people=current_people,
        scenes=scenes_input,
        photo_analyses_by_scene=pa_map,
        preserved_names=preserved_names,
    )

    final_people, updated_pa_map = apply_name_mapping(
        name_mapping=name_mapping,
        regrouped_people=regrouped_people,
        photo_analyses_by_scene=pa_map,
        preserved_people=preserved_people,
    )

    # 4. people.json 갱신
    all_stored_people = load_people()
    merged_stored = []
    seen_stored = set()
    for p in all_stored_people:
        old_name = p.get("name")
        new_name = name_mapping.get(old_name, old_name)
        p["name"] = new_name
        if new_name not in seen_stored:
            seen_stored.add(new_name)
            merged_stored.append(p)
    for cp in final_people:
        if cp["name"] not in seen_stored:
            seen_stored.add(cp["name"])
            merged_stored.append(cp)
        else:
            for mp in merged_stored:
                if mp["name"] == cp["name"]:
                    mp["features"] = cp["features"]
    save_people(merged_stored)

    # 5. 이야기 다시 쓰기 (Sonnet 글만 1회 호출)
    new_stories = analyzer.write_story_texts(
        folder_name=folder_name,
        scenes=scenes_input,
        photo_analyses_by_scene=updated_pa_map,
        people=final_people,
    )

    # 6. analysis_data 갱신 및 저장
    prev_usage = analysis_data.get("usage", {})
    total_usage = {}
    for m in set(list(prev_usage.keys()) + list(analyzer.usage.keys())):
        pu = prev_usage.get(m, {"input_tokens": 0, "output_tokens": 0})
        nu = analyzer.usage.get(m, {"input_tokens": 0, "output_tokens": 0})
        total_usage[m] = {
            "input_tokens": pu.get("input_tokens", 0) + nu.get("input_tokens", 0),
            "output_tokens": pu.get("output_tokens", 0) + nu.get("output_tokens", 0),
        }

    for sc in scenes_data:
        idx = sc.get("scene_idx", 1)
        if idx in updated_pa_map:
            sc["analyses"] = updated_pa_map[idx]
        if idx in new_stories:
            sc["story"] = new_stories[idx]

    analysis_data["regrouped"] = True
    analysis_data["regrouped_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    analysis_data["name_mapping"] = name_mapping
    analysis_data["people"] = final_people
    analysis_data["usage"] = total_usage
    analysis_data["regroup_usage"] = analyzer.usage

    folder_data_dir = TOOLS_DATA / folder_name
    analysis_file = folder_data_dir / "analysis.json"
    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(analysis_data, f, ensure_ascii=False, indent=2)

    publish_story(folder_name, folder_path, analysis_data, cfg)


def handle_list() -> None:
    """'이야기책 목록 보여줘': 만든 이야기책을 표로(AI 호출 없음)."""
    rows = []
    for analysis_file in sorted(TOOLS_DATA.glob("*/analysis.json")):
        try:
            data = json.loads(analysis_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = analysis_file.parent.name
        html_files = sorted(MYDATA_DIR.glob(f"*_{name}.html"), reverse=True)
        if not html_files:
            continue
        people = [p.get("name", "") for p in data.get("people", []) if p.get("name") and p.get("name") not in ("손님들", "Guests")]
        made = html_files[0].name[:8]
        rows.append((made, name, data.get("total_photos", 0), len(data.get("scenes", [])), people, html_files[0]))
    print(T("list_title"))
    if not rows:
        print(T("list_empty"))
        return
    print(T("list_count", n=len(rows)))
    print(T("list_head"))
    for made, name, total, scenes, people, html in sorted(rows, reverse=True):
        who = ", ".join(people[:6]) + (T("people_more", n=len(people) - 6) if len(people) > 6 else "")
        url = path_link(html.resolve()).split("](", 1)[1].rstrip(")")
        print(T("list_row", name=name, total=total, scenes=scenes, who=who or '-', made=f"{made[:4]}-{made[4:6]}-{made[6:]}", url=url))
    print(T("list_footer"))



def handle_video_list() -> None:
    """'영상 목록 보여줘': 만든 추억·성장 영상을 표로(AI 호출 없음)."""
    from video import media_duration
    files = sorted(MYDATA_DIR.glob("*.mp4"), reverse=True)
    print(T("vlist_title"))
    if not files:
        print(T("vlist_empty"))
        return
    print(T("vlist_count", n=len(files)))
    print(T("vlist_head"))
    for f in files:
        stamp, _, name = f.stem.partition("_")
        try:
            sec = media_duration(f)
            length = T("vlist_length", m=int(sec // 60), s=int(sec % 60)) if sec else "-"
        except Exception:
            length = "-"
        made = f"{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}" if len(stamp) >= 13 else "-"
        kind = T("kind_growth") if name.endswith("성장") else T("kind_memory")   # 성장 영상 파일 이름은 '..._이름성장.mp4'
        url = path_link(f.resolve()).split("](", 1)[1].rstrip(")")
        print(T("vlist_row", name=name or f.stem, kind=kind, length=length, mb=f"{f.stat().st_size / 1e6:.0f}", made=made, url=url))
    print(T("vlist_folder", link=path_link(MYDATA_DIR.resolve())))


def handle_show(folder_query: str) -> bool:
    """'○○ 사진 이야기책 보여줘': 이미 만든 이야기책(가장 최근 HTML)을 기본 브라우저로 연다. 없으면 False."""
    cfg = load_config()
    matched_path, candidates = find_photo_folder(folder_query, cfg.get("roots", []))
    name = matched_path.name if matched_path else folder_query
    html_files = sorted(MYDATA_DIR.glob(f"*_{name}.html"), reverse=True)
    if not html_files:
        return False
    system.open_path(str(html_files[0].resolve()))
    print(T("show_title", name=name))
    print(T("show_opened", link=path_link(html_files[0].resolve())))
    return True


def handle_search(query_text: str) -> None:
    """'사진 찾기': 여러 analysis.json 에서 키워드로 사진을 찾아 카드 마크다운을 표준 출력으로 낸다(AI 호출 없음)."""
    results = search_photos(query_text)
    card = format_search_card(query_text, results)
    sys.stdout.write(card)


def pick_folder(start: str, title: str = "") -> str:
    """폴더 고르기 창(맨 앞에 뜸). 고른 폴더 경로, 취소하면 빈 글."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(parent=root, initialdir=start or None, mustexist=True,
                                       title=title or T("pick_title"))
    finally:
        root.destroy()


def handle_story(folder_query: str, fresh: bool = False, regroup: bool = False) -> None:
    cfg = load_config()
    roots = cfg.get("roots", [])

    # 여러 폴더(쉼표, 또는 '와/과/하고' 로 이은 폴더가 모두 실제 폴더일 때)는 대기 줄로
    names = waiting.split_names(folder_query, lambda n: bool(find_photo_folder(n, roots)[0]))
    if len(names) > 1:
        handle_story_many(names, fresh)
        return
    folder_query = names[0] if names else folder_query

    matched_path, candidates = find_photo_folder(folder_query, roots) if folder_query else (None, [])
    if candidates or not matched_path:
        # 폴더를 못 찾았거나 여럿이면(또는 폴더 없이 말하면) 폴더 고르기 창을 띄운다(사용자 요청 2026-11-08)
        start = candidates[0].parent if candidates else (roots[0] if roots else "")
        if not folder_query:
            why, title = "", T("pick_title")
        elif candidates:
            why = T("why_similar", query=folder_query)
            title = T("pick_title_similar", query=folder_query)
        else:
            why = T("why_not_found", query=folder_query)
            title = T("pick_title_not_found", query=folder_query)
        picked = pick_folder(str(start), title)
        if not picked:
            print(T("story_plain_title"))
            print(T("pick_cancelled", why=why + ' ' if why else ''))
            return
        matched_path = Path(picked)
        if why:
            notice = T("picked_notice", why=why, name=matched_path.name)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _story_for_folder(matched_path, fresh, regroup)
            head, _, rest = buf.getvalue().partition("\n")   # 제목 줄 바로 아래에 알림 줄
            sys.stdout.write(f"{head}\n\n{notice}\n\n{rest.lstrip(chr(10))}")
            return
    _story_for_folder(matched_path, fresh, regroup)


def _story_for_folder(matched_path: Path, fresh: bool, regroup: bool) -> None:
    folder_name = matched_path.name
    analysis_file = TOOLS_DATA / folder_name / "analysis.json"

    if not fresh and analysis_file.exists():
        try:
            with open(analysis_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
        except (OSError, ValueError):
            cached_data = None
        if cached_data is not None:
            # 인물 정리는 'regroup':true 로 시킬 때와, regrouped 표시가 없는 옛 자료를 처음 볼 때만
            if regroup or not cached_data.get("regrouped", False):
                handle_regroup_story(folder_name, matched_path, cached_data)
            else:
                handle_cached_story(folder_name, matched_path, cached_data)
            return

    # 빠른 사진 수 세기 (EXIF 파싱 없이 확장자만 확인하여 네트워크 지연 최소화)
    try:
        photos = [f for f in matched_path.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS]
    except Exception as e:
        sys.stderr.write(T("photo_check_failed", err=e) + "\n")
        sys.exit(1)

    if not photos:
        sys.stderr.write(T("no_photo_files", path=matched_path) + "\n")
        sys.exit(1)

    photo_count = len(photos)

    # 다른 작업이 진행 중이면 거절하지 않고 대기 줄에 올린다
    lock_info = running_lock()
    if lock_info:
        current_folder = lock_info.get("folder", "")
        if current_folder == folder_name:
            print(T("already_running_title", folder=current_folder))
            print(T("already_running_story", folder=current_folder))
            return
        pos = waiting.enqueue(matched_path, photo_count)
        print(T("story_queue_title", name=folder_name) + "\n")
        print(T("queued_note", cur=current_folder, pos=pos))
        print(T("queued_footer"))
        return

    # 분리 프로세스로 worker 실행
    spawn_worker(matched_path)

    # 시작 카드 표준 출력
    est_minutes = max(1, round(photo_count / 50))
    print(T("story_start_title", name=folder_name) + "\n")
    print(T("start_line", path=matched_path, n=photo_count, lo=est_minutes, hi=est_minutes + 2))
    print(T("start_head"))
    print(T("start_row", name=folder_name, n=photo_count, lo=est_minutes, hi=est_minutes + 2))
    print(T("start_footer"))




def handle_video(folder_query: str) -> None:
    """'○○ 추억 영상 만들어줘': 저장된 분석으로 뒤에서 영상을 만든다(분리 프로세스, 이야기책과 같은 잠금).
    '여행사진1과 여행사진2로' 처럼 폴더가 둘 이상이면 찍은 시각 순서로 이어 한 편으로 만든다."""
    roots = load_config().get("roots", [])

    def resolve(n: str) -> str:
        """분석 폴더 이름(없으면 사진 폴더 이름). 못 찾으면 빈 글."""
        if (TOOLS_DATA / n).is_dir():
            return n
        matched, candidates = find_photo_folder(n, roots) if n else (None, [])
        return matched.name if matched and not candidates else ""

    names = [resolve(n) or n for n in waiting.split_names(folder_query, lambda n: bool(resolve(n)))]
    names = list(dict.fromkeys(names))
    for n in names:
        if not (TOOLS_DATA / n / "analysis.json").exists():
            if len(names) > 1 or resolve(n):
                print(T("video_no_story", name=n))
                return
            sys.stderr.write(T("video_no_analysis", query=folder_query, name=folder_query or T("folder_word")) + "\n")
            sys.exit(1)
    if not names:
        sys.stderr.write(T("video_no_analysis_any") + "\n")
        sys.exit(1)
    name = "+".join(names)

    if LOCK_FILE.exists():
        try:
            lock_info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            if is_process_running(lock_info.get("pid", 0)):
                cur = lock_info.get("folder", "")
                print(T("already_running_title", folder=cur))
                print(T("busy_other", cur=cur))
                return
            LOCK_FILE.unlink()
        except Exception:
            pass

    spawn_worker(Path(name), VIDEO_SCRIPT)
    print(T("video_start_title", names=' · '.join(names)))
    print(T("video_start"))


def handle_growth(person: str) -> None:
    """'○○ 성장 영상 만들어줘': 분석된 폴더들에서 그 사람 얼굴을 찍은 시각 순서로 이은 영상을 뒤에서 만든다(추억 영상과 같은 잠금)."""
    import growth
    person = person.strip()
    for tail in ("의 성장", "의", " 성장", "성장", "'s growth", " growth"):
        if person.endswith(tail) and len(person) > len(tail):
            person = person[:-len(tail)].strip()
    if not person:
        print(T("growth_who"))
        return
    photos, folders = growth.collect_photos(person)
    if not photos:
        sys.stderr.write(T("growth_no_photos", name=person) + "\n")
        sys.exit(1)
    lock = running_lock()
    if lock:
        cur = lock.get("folder", "")
        print(T("already_running_title", folder=cur))
        print(T("busy_other", cur=cur))
        return
    spawn_worker(Path(person), GROWTH_SCRIPT)
    print(T("growth_start_title", name=person))
    print(T("growth_start", folders=' · '.join(folders), n=len(photos), name=person))


def handle_memory(trigger: str) -> None:
    """'오늘의 추억 보여줘'·자동 실행: 이야기책 사진 한 장 카드(memory.py). 자동 실행은 때가 아니면 아무것도 내지 않는다."""
    import memory
    card = memory.show(trigger)
    if card:
        sys.stdout.buffer.write(card.encode("utf-8"))
        sys.stdout.flush()


def main() -> None:
    if len(sys.argv) < 2:
        handle_status()
        return

    raw_arg = sys.argv[1].strip()
    action = "story"
    folder = ""
    query = ""
    fresh = False
    regroup = False
    from_name = ""
    name = ""
    to_name = ""
    text = ""

    if raw_arg in ("status", "상태"):
        action = "status"
    elif raw_arg.startswith("{") and '"schedule"' in raw_arg:   # 자동 실행(매시 정각)은 오늘의 추억만
        try:
            if json.loads(raw_arg).get("trigger") == "schedule":
                handle_memory("schedule")
                return
        except ValueError:
            pass
    else:
        try:
            parsed = json.loads(raw_arg)
            if isinstance(parsed, dict):
                action = parsed.get("action", "")
                folder = parsed.get("folder", "")
                query = parsed.get("query", "")
                fresh = bool(parsed.get("fresh", False))
                regroup = bool(parsed.get("regroup", False))
                from_name = parsed.get("from", "")
                to_name = parsed.get("to", "")
                text = parsed.get("text", "")
                name = parsed.get("name", "")

                if not action:
                    low = text.lower()   # 영어 낱말(video, list …)은 한국어 낱말 옆에 함께 본다
                    if ("대기" in text and "비워" in text) or ("queue" in low and ("clear" in low or "empty" in low)):
                        action = "queue_clear"
                    elif ("영상" in text and "목록" in text) or ("video" in low and "list" in low):
                        action = "video_list"
                    elif ("성장" in text and "영상" in text) or ("growth" in low and "video" in low):
                        action = "growth"
                    elif "영상" in text or "video" in low:
                        action = "video"
                    elif "오늘의 추억" in text or "오늘의추억" in text or "추억 사진" in text or "memor" in low:
                        action = "memory"
                    elif (query or "사진 찾아" in text or "사진 찾기" in text or "나온 사진" in text
                          or ("find" in low and ("photo" in low or "picture" in low))):
                        action = "search"
                    elif ("목록" in text and "이야기책" in text) or ("storybook" in low and "list" in low):
                        action = "list"
                    elif "어디까지" in text or "상태" in text or "status" in low or "progress" in low or "how far" in low:
                        action = "status"
                    elif "회전" in text or "바로 세워" in text or "세워" in text or "rotate" in low:
                        action = "rotate"
                    elif folder or text:
                        action = "story"
                    else:
                        action = "status"
        except json.JSONDecodeError:
            try:
                import re
                fixed = re.sub(r'([{,])\s*([a-zA-Z0-9_]+)\s*:', r'\1"\2":', raw_arg)
                fixed = re.sub(r':\s*([a-zA-Z0-9_\uac00-\ud7a3]+)\s*([,}])', r':"\1"\2', fixed)
                fixed = fixed.replace(':"true"', ':true').replace(':"false"', ':false')
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    action = parsed.get("action", "")
                    folder = parsed.get("folder", "")
                    query = parsed.get("query", "")
                    fresh = bool(parsed.get("fresh", False))
                    regroup = bool(parsed.get("regroup", False))
                    from_name = parsed.get("from", "")
                    to_name = parsed.get("to", "")
                    text = parsed.get("text", "")
                    if not action and (query or "사진 찾아" in text or "사진 찾기" in text or "나온 사진" in text):
                        action = "search"
            except Exception:
                folder = raw_arg

    if action == "search":
        handle_search(query or text)
    elif action == "status":
        handle_status()
    elif action == "rotate":
        if not folder:
            sys.stderr.write(T("need_rotate_folder") + "\n")
            sys.exit(1)
        handle_rotate(folder)
    elif action == "rename":
        if not folder or not from_name or not to_name:
            sys.stderr.write(T("need_rename_args") + "\n")
            sys.exit(1)
        handle_rename(folder, from_name, to_name)
    elif action == "queue_clear":
        handle_queue_clear()
    elif action == "video":
        handle_video(folder)
    elif action == "growth":
        handle_growth(name or folder)
    elif action == "video_list":
        handle_video_list()
    elif action == "memory":
        handle_memory("quick")
    elif action == "list":
        handle_list()
    elif action == "story":
        if ("보여" in text or "show" in text.lower() or "open" in text.lower()) and not fresh and not regroup and handle_show(folder):
            return
        handle_story(folder, fresh=fresh, regroup=regroup)
    else:
        handle_status()


if __name__ == "__main__":
    main()
