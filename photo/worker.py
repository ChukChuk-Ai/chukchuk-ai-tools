"""사진 이야기책 백그라운드 워커 모듈."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import traceback

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))   # 같은 폴더의 모듈(common·worker …). sdk 는 common 이 찾는다

from ai import PhotoAnalyzer, apply_name_mapping
from common import TOOLS_DATA, T, load_config
from sdk import data, system
from reporter import build_story_html, format_finish_card
from scanner import group_scenes, scan_folder
from sheet import make_thumbnail_sheets

LOCK_FILE = TOOLS_DATA / "job.lock"
STATUS_FILE = TOOLS_DATA / "status.json"
PEOPLE_FILE = TOOLS_DATA / "people.json"


def is_process_running(pid: int) -> bool:
    """그 번호의 프로세스가 살아 있는가(sdk.system)."""
    return system.process_alive(pid)


def update_status(
    folder: str,
    folder_path: Path,
    status: str,
    step: int,
    total_steps: int,
    step_name: str,
    progress: str,
    started_at: str,
) -> None:
    TOOLS_DATA.mkdir(parents=True, exist_ok=True)
    data = {
        "status": status,
        "folder": folder,
        "folder_path": str(folder_path),
        "step": step,
        "total_steps": total_steps,
        "step_name": step_name,
        "progress": progress,
        "started_at": started_at,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_people() -> list[dict]:
    if PEOPLE_FILE.exists():
        try:
            with open(PEOPLE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_people(people: list[dict]) -> None:
    TOOLS_DATA.mkdir(parents=True, exist_ok=True)
    with open(PEOPLE_FILE, "w", encoding="utf-8") as f:
        json.dump(people, f, ensure_ascii=False, indent=2)


def protected_people(renamed: list[str] | None, *people_lists: list[dict]) -> tuple[set[str], list[dict]]:
    """사용자가 고친 이름(renamed_people, manual=True)과 그 인물 항목. AI 인물 정리가 덮어쓰지 못한다.
    people.json 은 부르는 순간 새로 읽는다(작업 도중 사용자가 고친 것까지 반영)."""
    names = set(renamed or [])
    pools = list(people_lists) + [load_people()]
    for pool in pools:
        for p in pool:
            if p.get("manual") and p.get("name"):
                names.add(p["name"])
    keep: list[dict] = []
    seen: set[str] = set()
    for pool in pools:
        for p in pool:
            n = p.get("name")
            if n in names and n not in seen:
                seen.add(n)
                keep.append(p)
    return names, keep


def run_worker(folder_path_str: str, keep_lock: bool = False) -> None:
    folder_path = Path(folder_path_str)
    folder_name = folder_path.name
    cfg = load_config()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 잠금 파일 생성
    TOOLS_DATA.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "pid": os.getpid(),
            "folder": folder_name,
            "folder_path": str(folder_path),
            "started_at": started_at,
        }, f, ensure_ascii=False)

    try:
        update_status(folder_name, folder_path, "running", 1, 6, T("step_scan"), T("prog_start"), started_at)

        # 1. 사진 스캔 & 정렬
        photos, skipped_media = scan_folder(folder_path)
        if not photos:
            raise RuntimeError(T("no_readable", path=folder_path))

        # 2. 장면 나누기
        scenes = group_scenes(photos, scene_gap_minutes=cfg.get("scene_gap_minutes", 30))

        # 3. 썸네일 판 생성
        update_status(folder_name, folder_path, "running", 2, 6, T("step_sheets"), T("prog_splitting", n=len(photos)), started_at)
        folder_data_dir = TOOLS_DATA / folder_name
        sheets_dir = folder_data_dir / "sheets"
        sheets = make_thumbnail_sheets(
            photos,
            sheets_dir,
            sheet_size=cfg.get("sheet_size", 40),
            cols=8,
            rows=5,
            max_dimension=1568,
        )

        # 4. 판 분류 (Haiku)
        update_status(folder_name, folder_path, "running", 3, 6, T("step_classify"), T("prog_sheets", i=0, n=len(sheets)), started_at)
        analyzer = PhotoAnalyzer(
            sort_model=cfg.get("sort_model", "cli-claude-haiku-5-5"),
            story_model=cfg.get("story_model", "cli-claude-sonnet-5-5"),
        )

        all_classification: dict[str, dict] = {}
        for s_idx, sh in enumerate(sheets):
            update_status(folder_name, folder_path, "running", 3, 6, T("step_classify"), T("prog_classifying", i=s_idx + 1, n=len(sheets)), started_at)
            res = analyzer.classify_sheet(sh["sheet_path"], sh["items"])
            all_classification.update(res)

        readable_count = sum(1 for c in all_classification.values() if c.get("readable"))

        # 5. 사진 고르기
        update_status(folder_name, folder_path, "running", 4, 7, T("step_select"), T("prog_select"), started_at)
        selected_photos = analyzer.select_photos_for_story(
            scenes,
            all_classification,
            max_per_scene=cfg.get("max_per_scene", 12),
            max_photos=cfg.get("max_photos", 60),
        )

        # 5-1. 회전 판단 (고른 사진만 Haiku, rotate 가 true 일 때만)
        rotate_enabled = bool(cfg.get("rotate", True))
        if rotate_enabled:
            update_status(folder_name, folder_path, "running", 5, 8, T("step_rotate"), T("prog_rotate", n=len(selected_photos)), started_at)
            rotations = analyzer.detect_rotations(selected_photos, sheets_dir=sheets_dir)
        else:
            rotations = {}
        for p in selected_photos:
            p["rotate"] = rotations.get(p["filename"], 0)
        for fn, r_val in rotations.items():
            if fn in all_classification:
                all_classification[fn]["rotate"] = r_val

        # 6. 자세히 보기 & 이야기 쓰기 (Sonnet)
        update_status(folder_name, folder_path, "running", 6, 8, T("step_detail"), T("prog_detail", n=len(selected_photos)), started_at)
        existing_people = load_people()
        folder_hint = folder_name

        photo_analyses_by_scene: dict[int, list[dict]] = {}
        current_people = existing_people

        for sc in scenes:
            s_idx = sc["scene_idx"]
            sc_photos = [p for p in selected_photos if any(sp["filename"] == p["filename"] for sp in sc["photos"])]
            if not sc_photos:
                sc_photos = sc["photos"][:1]

            analyses, current_people = analyzer.analyze_scene_photos(
                scene_idx=s_idx,
                scene_info=sc,
                photos=sc_photos,
                existing_people=current_people,
                folder_hint=folder_hint,
            )
            photo_analyses_by_scene[s_idx] = analyses

        # 6-1. 인물 정리 (Sonnet 글만 전송)
        update_status(folder_name, folder_path, "running", 6, 7, T("step_people"), T("prog_people"), started_at)
        old_renamed: list[str] = []
        try:
            old_renamed = json.loads((folder_data_dir / "analysis.json").read_text(encoding="utf-8")).get("renamed_people", [])
        except (OSError, ValueError):
            pass
        preserved_names, preserved_people = protected_people(old_renamed, current_people)
        name_mapping, regrouped_people = analyzer.regroup_people(
            folder_name=folder_name,
            people=current_people,
            scenes=scenes,
            photo_analyses_by_scene=photo_analyses_by_scene,
            preserved_names=preserved_names,
        )
        final_people, updated_photo_analyses = apply_name_mapping(
            name_mapping=name_mapping,
            regrouped_people=regrouped_people,
            photo_analyses_by_scene=photo_analyses_by_scene,
            preserved_people=preserved_people,
        )
        photo_analyses_by_scene = updated_photo_analyses
        current_people = final_people

        # 사람 기억 저장 (기존 people.json 업데이트)
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
        for cp in current_people:
            if cp["name"] not in seen_stored:
                seen_stored.add(cp["name"])
                merged_stored.append(cp)
            else:
                for mp in merged_stored:
                    if mp["name"] == cp["name"]:
                        mp["features"] = cp["features"]
        save_people(merged_stored)

        # 7. 이야기 작성
        update_status(folder_name, folder_path, "running", 7, 7, T("step_story"), T("prog_story"), started_at)
        stories = analyzer.write_story_texts(
            folder_name=folder_name,
            scenes=scenes,
            photo_analyses_by_scene=photo_analyses_by_scene,
            people=current_people,
        )

        # 8. 파일 저장 (analysis.json, HTML)
        for sc in scenes:
            s_idx = sc["scene_idx"]
            for an in photo_analyses_by_scene.get(s_idx, []):
                an["rotate"] = rotations.get(an.get("filename"), 0)

        analysis_data = {
            "folder": folder_name,
            "folder_path": str(folder_path),
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_photos": len(photos),
            "skipped_media": skipped_media,
            "readable_photos": readable_count,
            "selected_photos": [p["filename"] for p in selected_photos],
            "rotations": rotations,
            "scenes": [
                {
                    "scene_idx": sc["scene_idx"],
                    "start_time": sc["start_time_str"],
                    "end_time": sc["end_time_str"],
                    "photos_count": len(sc["photos"]),
                    "story": stories.get(sc["scene_idx"], {}),
                    "analyses": photo_analyses_by_scene.get(sc["scene_idx"], []),
                }
                for sc in scenes
            ],
            "people": current_people,
            "classification": all_classification,
            "usage": analyzer.usage,
            "regrouped": True,
            "name_mapping": name_mapping,
        }
        if old_renamed:
            analysis_data["renamed_people"] = [n for n in old_renamed if any(p.get("name") == n for p in current_people)]

        folder_data_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = folder_data_dir / "analysis.json"
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis_data, f, ensure_ascii=False, indent=2)

        # HTML 이야기책 생성
        html_path = build_story_html(
            folder_name=folder_name,
            folder_path=folder_path,
            scenes=scenes,
            selected_photos=selected_photos,
            photo_analyses_by_scene=photo_analyses_by_scene,
            stories=stories,
            people=current_people,
            total_photos=len(photos),
            readable_count=readable_count,
            usage_by_model=analyzer.usage,
        )

        # 8. 끝 카드 작성 (board/YYYYMMDD-HHMMSS-mmm_사진_이야기책_폴더이름.md)
        card_content = format_finish_card(
            folder_name=folder_name,
            folder_path=folder_path,
            total_count=len(photos),
            readable_count=readable_count,
            selected_count=len(selected_photos),
            scenes=scenes,
            stories=stories,
            photo_analyses_by_scene=photo_analyses_by_scene,
            people=current_people,
            html_path=html_path,
        )

        data.notify(card_content, telegram=bool(cfg.get("telegram", True)))   # 앱이 카드·텔레그램으로 알린다

        # 상태 완료 갱신
        update_status(folder_name, folder_path, "completed", 6, 6, T("step_done"), T("prog_done"), started_at)

    except Exception as e:
        err_msg = str(e)
        traceback.print_exc()

        # 실패 카드
        fail_content = T("story_failed", name=folder_name, path=folder_path, err=err_msg)
        data.notify(fail_content, telegram=bool(cfg.get("telegram", True)))

        update_status(folder_name, folder_path, "failed", 0, 6, T("step_failed"), err_msg, started_at)

    finally:
        # 잠금 해제(줄 처리 중이면 다음 폴더로 이어가려고 keep_lock 으로 둔다)
        if not keep_lock and LOCK_FILE.exists():
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass


def run_worker_chain(first_path: str) -> None:
    """한 폴더를 끝내면(성공이든 실패든 끝 카드를 쓴 뒤) 대기 줄에서 다음 폴더를 꺼내 이어서 처리한다."""
    from waiting import pop_next

    path = first_path
    while path:
        run_worker(path, keep_lock=True)
        nxt = pop_next()
        if not nxt:
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass
            nxt = pop_next()   # 잠금을 지우는 사이에 올라온 폴더가 있으면 이어간다
        path = nxt["folder_path"] if nxt else ""


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_worker_chain(sys.argv[1])
