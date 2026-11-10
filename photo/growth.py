"""성장 영상: 한 사람의 얼굴을 분석된 폴더들에서 모아 찍은 시각 순서로 이어 붙인 영상(교차 페이드·날짜 자막·배경음악).

얼굴 찾기는 faces.py(OpenCV YuNet·SFace, 상업 이용이 되는 라이선스)가 이 도구 환경에서 한다. 사진을 보내는 모델 호출은 없다.
SFace 지문의 닮음 기준은 OpenCV 권장값(같은 사람 코사인 0.363)에 여유를 둔 값이다. 실제 사진으로 고쳐 맞춘다.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from common import MYDATA_DIR, TOOLS_DATA, T, load_config, strip_guess  # noqa: E402
from sdk import data, system  # noqa: E402

FACES_DIR = TOOLS_DATA / "faces"
W, H, FPS = 1280, 720, 25
EYE_DIST = 84                 # 영상 속 두 눈 사이(px). 얼굴이 화면 높이의 4분의 1 남짓
EYE_MID = (W / 2, 290.0)      # 두 눈 한가운데가 놓일 자리
MIN_FACE_W = 64               # 원본에서 얼굴 너비가 이보다 작으면 쓰지 않는다(너무 흐려짐)
MIN_DET = 0.6                 # YuNet 점수
ACCEPT_SIM = 0.40             # 그 사람 얼굴 지문과 이만큼은 닮아야 한다(SFace 코사인, 같은 사람 기준 0.363)
MIN_MARGIN = 0.08             # 한 장에 여러 얼굴이면 둘째와 이만큼 차이가 나야 한다
SEED_KEEP = 0.30              # 기준 얼굴을 만들 때 평균과 이만큼은 닮아야 남긴다
SURE_SIM, SURE_MARGIN = 0.55, 0.12   # 여러 얼굴 사진에서 아주 확실한 것(기준에 보탬)
MAX_TILT = 30.0               # 눈 선이 이보다 기울면 쓰지 않는다(도)
TARGET_HI = 120.0             # 영상 길이 상한 목표(초). 하한은 사진 수에 따라(한 장 최대 4초)
NO_WINDOW = system.NO_WINDOW
FONT_BOLD = str(system.document_font()[2])   # 한글이 되는 굵은 글꼴(OS별)


# ---------- 순수 계산(모델·ffmpeg 없이 시험) ----------

def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _unit_mean(vs: list[list[float]]) -> list[float]:
    m = [sum(c) / len(vs) for c in zip(*vs)]
    n = math.sqrt(sum(x * x for x in m)) or 1.0
    return [x / n for x in m]


def _face_w(f: dict) -> float:
    return f["bbox"][2] - f["bbox"][0]


def person_in(analysis_people: list[str], name: str) -> bool:
    return any(strip_guess(p) == name for p in analysis_people)   # '(추정)'·' (guess)' 를 떼고 비교


def eye_tilt_deg(kps: list[list[float]]) -> float:
    (x0, y0), (x1, y1) = kps[0], kps[1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def align_coeffs(kps: list[list[float]], out_mid: tuple[float, float] = EYE_MID, eye_dist: float = EYE_DIST) -> tuple[float, ...]:
    """PIL Image.transform(AFFINE) 계수: 출력 좌표 → 원본 좌표. 두 눈을 수평으로 눕히고 눈 사이를 eye_dist, 중점을 out_mid 에."""
    (x0, y0), (x1, y1) = kps[0], kps[1]
    d = math.hypot(x1 - x0, y1 - y0) or 1.0
    s = eye_dist / d
    phi = -math.atan2(y1 - y0, x1 - x0)
    c, sn = math.cos(phi), math.sin(phi)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    ox, oy = out_mid
    a, b = c / s, sn / s
    dd, e = -sn / s, c / s
    return (a, b, mx - a * ox - b * oy, dd, e, my - dd * ox - e * oy)


def map_point(coeffs: tuple[float, ...], ox: float, oy: float) -> tuple[float, float]:
    a, b, c, d, e, f = coeffs
    return a * ox + b * oy + c, d * ox + e * oy + f


def pick_person_faces(photos: list[dict], faces: dict) -> tuple[list[dict], list[dict]]:
    """photos: [{key, folder, ...}], faces: faces.py 결과. 그 사람 얼굴만 골라 (뽑힌 것, 뺀 것).
    뽑힌 것 = photo 에 face·sim 을 더한 것, 뺀 것 = photo 에 reason 을 더한 것.
    방법: 폴더마다(나이·옷이 같은 무리) 얼굴이 하나뿐인 사진들의 얼굴 지문 평균을 기준으로 삼고
    (이상한 것은 빼고 다시 평균), 여러 얼굴 사진에서는 기준과 가장 닮은 얼굴을 고른다. 애매하면 뺀다."""
    def valid(key: str) -> list[dict]:
        return [f for f in faces.get(key, {}).get("faces", []) if f["det"] >= MIN_DET]

    by_folder: dict[str, list[dict]] = {}
    for p in photos:
        by_folder.setdefault(p["folder"], []).append(p)

    def seeds_of(ps: list[dict]) -> list[list[float]]:
        out = []
        for p in ps:
            fs = valid(p["key"])
            if len(fs) == 1 and _face_w(fs[0]) >= MIN_FACE_W:
                out.append(fs[0]["emb"])
        return out

    def prototype(vs: list[list[float]]) -> list[float] | None:
        if not vs:
            return None
        m = _unit_mean(vs)
        if len(vs) >= 3:   # 맞지 않는 것(다른 사람이 혼자 찍힌 사진)을 빼고 다시
            keep = [v for v in vs if _dot(v, m) >= SEED_KEEP]
            if keep and len(keep) < len(vs):
                m = _unit_mean(keep)
        return m

    all_seeds = [v for ps in by_folder.values() for v in seeds_of(ps)]
    protos: dict[str, list[float] | None] = {}
    for folder, ps in by_folder.items():
        seeds = seeds_of(ps)
        base = prototype(seeds) if len(seeds) >= 2 else prototype(all_seeds)
        # 한 번 더: 여러 얼굴 사진 중 아주 확실한 것(SURE_SIM 이상, 둘째와 SURE_MARGIN 이상 차이)을 기준에 보탠다
        if base:
            extra = []
            for p in ps:
                fs = valid(p["key"])
                if len(fs) > 1:
                    sims = sorted(((_dot(f["emb"], base), f) for f in fs), key=lambda x: -x[0])
                    if sims[0][0] >= SURE_SIM and sims[0][0] - sims[1][0] >= SURE_MARGIN and _face_w(sims[0][1]) >= MIN_FACE_W:
                        extra.append(sims[0][1]["emb"])
            if extra:
                base = prototype(seeds + extra) or base
        protos[folder] = base

    chosen, dropped = [], []
    for p in photos:
        proto = protos.get(p["folder"])
        fs = valid(p["key"])
        if p["key"] not in faces:
            dropped.append(dict(p, reason=T("drop_unreadable")))
            continue
        if not fs:
            dropped.append(dict(p, reason=T("drop_no_face")))
            continue
        if proto is None:
            dropped.append(dict(p, reason=T("drop_no_base")))
            continue
        scored = sorted(((_dot(f["emb"], proto), f) for f in fs), key=lambda x: -x[0])
        sim, face = scored[0]
        if sim < ACCEPT_SIM:
            dropped.append(dict(p, reason=T("drop_unsure")))
        elif len(scored) > 1 and sim - scored[1][0] < MIN_MARGIN:
            dropped.append(dict(p, reason=T("drop_unsure")))
        elif _face_w(face) < MIN_FACE_W:
            dropped.append(dict(p, reason=T("drop_small")))
        elif abs(eye_tilt_deg(face["kps"])) > MAX_TILT:
            dropped.append(dict(p, reason=T("drop_tilted")))
        else:
            chosen.append(dict(p, face=face, sim=sim))
    return chosen, dropped


def order_photos(items: list[dict]) -> list[dict]:
    """찍은 시각 순서(같으면 폴더 순서, 파일 이름)."""
    return sorted(items, key=lambda p: (p["taken"], p.get("folder_idx", 0), p["fname"]))


def plan_timeline(n: int) -> tuple[float, float, list[int]]:
    """사진 n장 → (한 장 보이는 초, 교차 페이드 초, 쓸 사진 순번들). 길이 60~120초를 겨냥하되 너무 많으면 고르게 덜어낸다."""
    step = min(4.0, max(2.0, 90.0 / max(n, 1)))
    keep = list(range(n))
    cap = int(TARGET_HI / step)
    if n > cap:
        keep = [int(i * n / cap) for i in range(cap)]
    return step, round(min(0.9, step * 0.4), 2), keep


def smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def date_text(t: datetime) -> str:
    return t.strftime("%Y.%m.%d")


def output_name(name: str, now: datetime | None = None) -> str:
    return f"{(now or datetime.now()):%Y%m%d-%H%M}_{name}성장.mp4"


# ---------- 얼굴 찾기(전용 가상환경 호출) ----------

def collect_photos(name: str) -> tuple[list[dict], list[str]]:
    """분석된 폴더들에서 name 이 나온 사진 목록과 폴더 이름들."""
    photos, folders = [], []
    for fi, d in enumerate(sorted(p for p in TOOLS_DATA.iterdir() if (p / "analysis.json").exists())):
        a = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        rot = a.get("rotations", {})
        fp = Path(a.get("folder_path", ""))
        got = 0
        for sc in a.get("scenes", []):
            for x in sc.get("analyses", []):
                if not person_in(x.get("people_present", []), name):
                    continue
                fn = x["filename"]
                try:
                    taken = datetime.strptime(x.get("taken", ""), "%Y-%m-%d %H:%M:%S")
                except ValueError:   # 이야기책 분석에는 찍은 시각이 없다: 사진 파일(EXIF·파일 시각)에서 읽는다
                    from scanner import get_photo_timestamp
                    if not (fp / fn).is_file():
                        continue
                    taken = get_photo_timestamp(fp / fn)
                    if taken == datetime.min:
                        continue
                photos.append({"key": f"{d.name}/{fn}", "folder": d.name, "folder_idx": fi, "fname": fn, "path": str(fp / fn),
                               "rotate": rot.get(fn, x.get("rotate", 0)), "taken": taken})
                got += 1
        if got:
            folders.append(d.name)
    return photos, folders


def run_face_finder(photos: list[dict], tmp: Path) -> dict:
    """사진들의 얼굴(faces.py, 이 도구 환경의 OpenCV). 처음이면 모델을 받는다."""
    from faces import FaceError, find_faces
    try:
        return find_faces([{"key": p["key"], "path": p["path"], "rotate": p["rotate"]} for p in photos],
                          FACES_DIR / "cache.json", FACES_DIR / "models")
    except FaceError as exc:
        raise RuntimeError(str(exc)) from None


# ---------- 그림 ----------

def load_oriented(path: str, rotate: int) -> Image.Image:
    from reporter import apply_rotation
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im)
        if rotate % 360:
            img = apply_rotation(img, rotate)
        return img.convert("RGB")


def eye_size_for(img_w: int, img_h: int, eye_px: float) -> float:
    """영상 속 눈 사이 길이. 기본 EYE_DIST 이지만, 원본 얼굴이 아주 커서 사진이 화면보다 작아지면(작은 액자처럼 됨)
    사진이 화면을 덮을 만큼까지 키운다(최대 EYE_DIST 의 2.2배)."""
    cover = 1.05 * max(W / img_w, H / img_h) * eye_px
    return min(max(EYE_DIST, cover), EYE_DIST * 2.2)


def render_aligned(img: Image.Image, kps: list[list[float]]) -> Image.Image:
    """눈 위치를 맞춘 1280x720 장면: 흐린 배경 + 정렬된 사진(빈 곳은 배경이 비침)."""
    d = math.hypot(kps[1][0] - kps[0][0], kps[1][1] - kps[0][1]) or 1.0
    eye = eye_size_for(img.width, img.height, d)
    s = eye / d
    pre = min(1.0, 2 * s)   # 축소해서 쓸 때는 먼저 줄여 계단 현상을 막는다
    if pre < 1.0:
        img = img.resize((max(1, round(img.width * pre)), max(1, round(img.height * pre))), Image.LANCZOS)
        kps = [[x * pre, y * pre] for x, y in kps]
    co = align_coeffs(kps, eye_dist=eye)
    fg = img.transform((W, H), Image.AFFINE, co, resample=Image.BICUBIC)
    mask = Image.new("L", img.size, 255).transform((W, H), Image.AFFINE, co, resample=Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
    bg = ImageOps.fit(img, (W, H)).filter(ImageFilter.GaussianBlur(30))
    bg = bg.point(lambda v: int(v * 0.5))
    bg.paste(fg, (0, 0), mask)
    return bg


def add_texts(frame: Image.Image, date: str, title: str = "") -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    f = ImageFont.truetype(FONT_BOLD, 40)
    tw = dr.textlength(date, font=f)
    dr.rounded_rectangle((W / 2 - tw / 2 - 26, H - 92, W / 2 + tw / 2 + 26, H - 28), radius=14, fill=(0, 0, 0, 140))
    dr.text((W / 2, H - 80), date, font=f, fill=(255, 255, 255, 255), anchor="ma")
    if title:
        ft = ImageFont.truetype(FONT_BOLD, 56)
        tw = dr.textlength(title, font=ft)
        dr.rounded_rectangle((W / 2 - tw / 2 - 30, 36, W / 2 + tw / 2 + 30, 118), radius=18, fill=(0, 0, 0, 120))
        dr.text((W / 2 + 2, 48 + 2), title, font=ft, fill=(0, 0, 0, 255), anchor="ma")
        dr.text((W / 2, 48), title, font=ft, fill=(255, 236, 170, 255), anchor="ma")
    return Image.alpha_composite(frame.convert("RGBA"), ov).convert("RGB")


def write_frames(frames: list[Image.Image], step: float, fade: float, out: Path) -> float:
    """사진 한 장을 step 초 보이고 마지막 fade 초에 다음 장으로 교차 페이드. 영상 길이(초)를 돌려준다."""
    from video import ffmpeg_exe
    hold_n, fade_n = round((step - fade) * FPS), max(1, round(fade * FPS))
    p = subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                          "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)],
                         stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    n = 0
    try:
        for i, fr in enumerate(frames):
            raw = fr.tobytes()
            last = i == len(frames) - 1
            for _ in range(hold_n + (fade_n if last else 0)):
                p.stdin.write(raw)
                n += 1
            if not last:
                for k in range(fade_n):
                    p.stdin.write(Image.blend(fr, frames[i + 1], smoothstep((k + 1) / (fade_n + 1))).tobytes())
                    n += 1
        p.stdin.close()
        err = p.stderr.read().decode("utf-8", "replace")
        if p.wait() != 0:
            raise RuntimeError(T("ffmpeg_failed", err=err[-500:]))
    finally:
        if p.poll() is None:
            p.kill()
    return n / FPS


def add_music(silent: Path, seconds: float, title_list: list[str], name: str, cfg: dict, out: Path) -> tuple[list[str], str, str]:
    """배경음악을 고르고(글 호출 한 번) 영상에 깐다. (곡들, 이유, 못 깐 사유)."""
    from video import list_music, media_duration, pick_music, run_ffmpeg
    music_dir = str(cfg.get("music_folder", "") or "")
    songs, reason, note = [], "", ""
    if music_dir:
        try:
            songs, reason = pick_music(T("growth_label", name=name), [{"title": t} for t in title_list], list_music(music_dir), cfg)
            if not songs:
                note = T("music_not_picked")
        except Exception as e:
            note = T("music_failed", err=e)
    total_s = f"{seconds:.2f}"
    if not songs:
        shutil.move(str(silent), str(out))
        return [], "", note or T("music_folder_empty")
    ins, lens, paths = ["-i", str(silent)], 0.0, []
    while lens < seconds + 4 and len(paths) < 8:
        for s in songs:
            paths.append(Path(music_dir) / s)
            lens += media_duration(paths[-1])
    for p in paths:
        ins += ["-i", str(p)]
    if len(paths) == 1:
        mix = "[1:a]anull[m]"
    else:
        steps, prev = [], "1:a"
        for i in range(1, len(paths)):
            steps.append(f"[{prev}][{i + 1}:a]acrossfade=d=3[x{i}]")
            prev = f"x{i}"
        mix = ";".join(steps) + f";[{prev}]anull[m]"
    fc = (f"{mix};[m]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{total_s},volume=0.8,"
          f"afade=t=in:d=2,afade=t=out:st={max(seconds - 4, 0):.2f}:d=4[aud]")
    run_ffmpeg([*ins, "-filter_complex", fc, "-map", "0:v", "-map", "[aud]", "-t", total_s, "-c:v", "copy",
                "-c:a", "aac", "-b:a", "160k", str(out)])
    return songs, reason, ""


# ---------- 본 작업 ----------

def make_growth(name: str, cfg: dict, report) -> dict:
    from video import media_duration
    photos, folders = collect_photos(name)
    if not photos:
        raise RuntimeError(T("growth_no_photos_short", name=name))
    MYDATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="growth_", dir=str(TOOLS_DATA)))
    try:
        report(1, T("g_step_faces"), T("g_prog_faces", n=len(photos)))
        faces = run_face_finder(photos, tmp)
        chosen, dropped = pick_person_faces(photos, faces)
        chosen = order_photos(chosen)
        if len(chosen) < 2:
            raise RuntimeError(T("growth_too_few", name=name, n=len(chosen)))
        step, fade, keep = plan_timeline(len(chosen))
        used = [chosen[i] for i in keep]
        frames = []
        for i, p in enumerate(used):
            report(2, T("g_step_align"), T("g_prog_align", i=i + 1, n=len(used)))
            fr = render_aligned(load_oriented(p["path"], p["rotate"]), p["face"]["kps"])
            frames.append(add_texts(fr, date_text(p["taken"]), T("growth_video_title", name=name) if i == 0 else ""))
        report(3, T("g_step_merge"), T("g_prog_merge"))
        silent = tmp / "silent.mp4"
        seconds = write_frames(frames, step, fade, silent)
        titles, seen = [], set()
        for p in used:
            if p["folder"] not in seen:
                seen.add(p["folder"])
                titles.append(f"{p['folder']} ({p['taken']:%Y.%m})")
        out = MYDATA_DIR / output_name(name)
        songs, reason, music_note = add_music(silent, seconds, titles, name, cfg, out)
        reasons: dict[str, int] = {}
        for d in dropped:
            reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
        per_folder: dict[str, int] = {}
        for p in used:
            per_folder[p["folder"]] = per_folder.get(p["folder"], 0) + 1
        return {"path": out, "seconds": media_duration(out), "size": out.stat().st_size, "used": len(used), "found": len(photos),
                "skipped_by_length": len(chosen) - len(used), "dropped": len(dropped), "reasons": reasons, "per_folder": per_folder,
                "first": used[0]["taken"], "last": used[-1]["taken"], "music": songs, "reason": reason, "music_note": music_note}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def format_growth_card(name: str, info: dict) -> str:
    from reporter import path_link
    from video import clock
    mb = info["size"] / 1048576
    music = " + ".join(Path(s).stem for s in info["music"]) if info["music"] else T("none_with", note=info["music_note"])
    if info["music"] and info["reason"]:
        music += f" ({info['reason']})"
    why = " · ".join(f"{k} {v}" for k, v in info["reasons"].items()) or T("none_word")
    folders = " · ".join(T("growth_per_folder", folder=k, n=v) for k, v in info["per_folder"].items())
    extra = T("growth_trimmed", n=info["skipped_by_length"]) if info["skipped_by_length"] else ""
    return T("growth_card", name=name, used=info["used"], length=clock(info["seconds"]), first=f"{info['first']:%Y.%m.%d}",
             last=f"{info['last']:%Y.%m.%d}", dropped=info["dropped"], video=path_link(Path(info["path"]).resolve()), mb=f"{mb:.1f}",
             folder=path_link(MYDATA_DIR.resolve()), folders=folders, why=why, extra=extra, music=music, found=info["found"])


def run_growth_worker(name: str) -> None:
    """분리 프로세스 진입점: 영상을 만들고 끝나면 앱에 알린다(sdk.data.notify). 잠금·status 는 이야기책·추억 영상과 같은 파일."""
    from video import clock
    from worker import LOCK_FILE, update_status

    cfg = load_config()
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = T("growth_label", name=name)
    TOOLS_DATA.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "folder": label, "folder_path": "", "started_at": started, "kind": "growth"},
                                    ensure_ascii=False), encoding="utf-8")
    try:
        def report(step: int, step_name: str, progress: str) -> None:
            update_status(label, Path(""), "running", step, 3, T("growth_status", step=step_name), progress, started)

        report(1, T("step_prepare"), "")
        info = make_growth(name, cfg, report)
        card = format_growth_card(name, info)
        data.notify(card, telegram=bool(cfg.get("telegram", True)))
        update_status(label, Path(""), "completed", 3, 3, T("step_done"), T("prog_video_len", length=clock(info["seconds"])), started)
    except Exception as e:
        traceback.print_exc()
        data.notify(T("growth_failed", name=name, err=e), telegram=bool(cfg.get("telegram", True)))
        update_status(label, Path(""), "failed", 0, 3, T("step_failed"), str(e), started)
    finally:
        try:
            if json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                LOCK_FILE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_growth_worker(sys.argv[1])
