"""추억 영상 만들기: 저장된 analysis.json 의 고른 사진 + 장면 이야기로 1280x720 MP4.

사진을 보내는 모델 호출은 없다. 글만 보내는 호출은 배경음악 고르기 한 번뿐이다('글 작업' 모델).
내레이션은 edge-tts(도구 설정의 목소리, 비우면 화면 언어의 기본 목소리), 실패하면 OS 내장 음성(sdk.system).
edge-tts 는 Microsoft Edge 의 읽어 주기 목소리를 빌려 쓰는 비공식 라이브러리다(인터넷 필요).
ffmpeg 는 도구 환경의 imageio-ffmpeg 안의 실행 파일을 쓴다(시스템 설치 없음).
"""
from __future__ import annotations

import asyncio
import json
import re
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

from common import MYDATA_DIR, TOOLS_DATA, T, load_config, output_language  # noqa: E402
from cli import ask_text  # noqa: E402
from reporter import apply_rotation  # noqa: E402
from sdk import config, data, system  # noqa: E402

W, H, FPS = 1280, 720, 25
NO_WINDOW = system.NO_WINDOW
_, _FONT, _FONT_BOLD = system.document_font()   # 한글이 되는 글꼴(OS별)
FONT, FONT_BOLD = str(_FONT), str(_FONT_BOLD)
EDGE_VOICES = {"ko": "ko-KR-SunHiNeural", "en": "en-US-JennyNeural"}   # 도구 설정에 목소리가 없을 때 화면 언어의 기본
CULTURES = {"ko": "ko-KR", "en": "en-"}                                 # 내장 음성에서 찾을 언어
LEAD, GAP, TAIL = 0.3, 0.35, 0.7   # 장면 앞 쉼, 문장 사이 쉼, 장면 끝 쉼(초)
MULTI_LO, MULTI_HI = 180.0, 300.0  # 여러 폴더를 한 편으로 만들 때 전체 길이 목표(초)


# ---------- 글 나누기·길이 계산(ffmpeg 없이 시험 가능) ----------

def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+", text.strip()) if s.strip()]


def caption_chunks(text: str, max_chars: int = 50) -> list[str]:
    """이야기 글을 자막(과 내레이션) 한 토막씩으로. 문장 단위, 너무 길면 쉼표에서 나눈다."""
    chunks: list[str] = []
    for sent in split_sentences(text):
        if len(sent) <= max_chars:
            chunks.append(sent)
            continue
        parts = re.split(r"(?<=,)\s+", sent)
        cur = ""
        for part in parts:
            if cur and len(cur) + 1 + len(part) > max_chars:
                chunks.append(cur)
                cur = part
            else:
                cur = f"{cur} {part}".strip()
        if cur:
            chunks.append(cur)
    return chunks


def scene_length(chunk_secs: list[float], photo_count: int, min_photo: float) -> float:
    """장면 길이 = 내레이션 길이(쉼 포함)와 사진 수x최소 초 중 긴 쪽."""
    narr = LEAD + sum(s + GAP for s in chunk_secs) + TAIL
    return max(narr, photo_count * min_photo)


def choose_min_photo_sec(scenes: list[tuple[list[float], int]], lo: float = 120.0, hi: float = 180.0) -> float:
    """전체가 lo~hi초가 되도록 사진 한 장의 최소 초를 고른다(4초에서 시작해 길면 줄이고 짧으면 늘림)."""
    def total(m: float) -> float:
        return sum(scene_length(c, n, m) for c, n in scenes)

    m = 4.0
    while total(m) > hi and m > 2.5:
        m -= 0.5
    while total(m) < lo and m < 6.0:
        m += 0.5
    return m


def clock(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def parse_duration(ffmpeg_stderr: str) -> float:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", ffmpeg_stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


# ---------- ffmpeg ----------

def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> str:
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, creationflags=NO_WINDOW)
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError(T("ffmpeg_failed", err=err[-600:]))
    return err


def media_duration(path: Path) -> float:
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, creationflags=NO_WINDOW)
    return parse_duration(p.stderr.decode("utf-8", "replace"))


# ---------- 내레이션(대화 음성 → 실패하면 윈도우 음성) ----------

def edge_voice_name() -> str:
    """도구 설정의 목소리(비우면 화면 언어의 기본 목소리)."""
    return str(load_config().get("edge_voice") or "").strip() or EDGE_VOICES.get(config.language(), EDGE_VOICES["en"])


async def _edge_all(items: list[tuple[str, Path]], voice: str) -> None:
    import edge_tts
    for text, path in items:
        await edge_tts.Communicate(text, voice).save(str(path))


def synth_sapi(items: list[tuple[str, Path]], tmp: Path) -> tuple[str, bool]:
    """OS 내장 음성으로 wav 만들기(sdk.system). (목소리 이름, 화면 언어 목소리 있음)"""
    try:
        return system.speak_to_files(items, CULTURES.get(config.language(), "en-"), tmp)
    except RuntimeError as exc:
        raise RuntimeError(T("sapi_failed", err=exc)) from None


def synthesize(texts: list[str], tmp: Path) -> tuple[list[Path], str]:
    """texts 마다 소리 파일 하나. 돌려주는 값: (파일들, 카드에 적을 목소리 설명)."""
    voice = edge_voice_name()
    mp3 = [tmp / f"n{i:03d}.mp3" for i in range(len(texts))]
    try:
        asyncio.run(_edge_all(list(zip(texts, mp3)), voice))
        if all(p.exists() and p.stat().st_size > 0 for p in mp3):
            return mp3, f"{voice}(edge-tts)"
        raise RuntimeError(T("empty_audio"))
    except Exception as e:
        print(T("edge_failed", err=e), file=sys.stderr)
    wav = [tmp / f"n{i:03d}.wav" for i in range(len(texts))]
    name, ko = synth_sapi(list(zip(texts, wav)), tmp)
    note = T("voice_fallback", name=name)
    if not ko:
        note += T("voice_no_lang")
    return wav, note


# ---------- 그림: 사진 틀, 자막 ----------

def make_canvas(path: Path, rotate: int, out: Path) -> None:
    """1920x1080 틀: 흐린 배경 + 가운데 사진(회전·EXIF 보정). 원본은 읽기만 한다."""
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im)
        if rotate % 360:
            img = apply_rotation(img, rotate)
        img = img.convert("RGB")
    cw, ch = W * 3 // 2, H * 3 // 2
    bg = ImageOps.fit(img, (cw, ch)).filter(ImageFilter.GaussianBlur(28))
    bg = Image.eval(bg, lambda v: int(v * 0.55))
    fg = ImageOps.contain(img, (cw, ch))
    bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
    bg.save(out, quality=92)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def make_caption_png(text: str, out: Path) -> None:
    """아래쪽 짧은 자막(반투명 검은 띠 + 흰 글자). 전체 화면 크기 투명 PNG."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 34)
    lines = _wrap(draw, text, font, W - 160)
    lh = 46
    box_h = lh * len(lines) + 24
    top = H - 34 - box_h
    draw.rounded_rectangle((60, top, W - 60, H - 34), radius=14, fill=(0, 0, 0, 150))
    for i, line in enumerate(lines):
        draw.text((W // 2, top + 12 + i * lh), line, font=font, fill=(255, 255, 255, 255), anchor="ma")
    img.save(out)


def make_title_png(text: str, out: Path) -> None:
    """장면 제목: 위쪽 가운데 큰 글씨."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_BOLD, 52)
    lines = _wrap(draw, text, font, W - 200)[:2]
    lh = 70
    top = 60
    draw.rounded_rectangle((70, top - 16, W - 70, top + lh * len(lines) + 12), radius=18, fill=(0, 0, 0, 120))
    for i, line in enumerate(lines):
        draw.text((W // 2 + 2, top + i * lh + 2), line, font=font, fill=(0, 0, 0, 255), anchor="ma")
        draw.text((W // 2, top + i * lh), line, font=font, fill=(255, 236, 170, 255), anchor="ma")
    img.save(out)


def zoom_filter(idx: int, frames: int) -> str:
    """사진마다 다른 움직임(확대, 축소, 좌→우, 우→좌)."""
    d = max(frames, 2)
    center = "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
    kinds = [
        f"z='1+0.12*on/{d}':{center}",
        f"z='1.12-0.12*on/{d}':{center}",
        f"z='1.1':x='(iw-iw/zoom)*on/{d}':y='ih/2-ih/zoom/2'",
        f"z='1.1':x='(iw-iw/zoom)*(1-on/{d})':y='ih/2-ih/zoom/2'",
    ]
    return f"zoompan={kinds[idx % 4]}:d={d}:s={W}x{H}:fps={FPS},format=yuv420p"


# ---------- 배경음악 ----------

def list_music(folder: str) -> list[str]:
    if not folder:
        return []
    try:
        return sorted(p.name for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() == ".mp3")
    except OSError:
        return []


def pick_music(folder_name: str, scenes: list[dict], names: list[str], cfg: dict) -> tuple[list[str], str]:
    """파일 이름 목록과 장면 제목을 글로만 보내 어울리는 곡 1~2곡과 이유 한 줄을 받는다."""
    if not names:
        return [], ""
    titles = [s["title"] for s in scenes]
    prompt = (
        f"추억 영상('{folder_name}')에 깔 배경음악을 아래 곡 목록에서 1~2곡 고르세요.\n"
        f"영상 장면 제목 순서: {json.dumps(titles, ensure_ascii=False)}\n"
        "조건: 가족·친구와의 추억, 여행·나들이에 어울리는 따뜻하고 밝은 곡. 슬픈 이별 노래, 격한 곡은 피하세요. "
        "곡이 둘이면 장면 흐름(앞부분, 뒷부분)에 맞게 순서를 정하세요.\n"
        "목록에 있는 파일 이름 그대로 쓰고, 고른 이유는 한국어 한 줄로 쓰세요. JSON만 답하세요:\n"
        '{"songs": ["파일이름.mp3"], "reason": "..."}\n\n곡 목록:\n' + "\n".join(names)
        + "\n\n" + output_language("고른 이유")   # 이유를 화면 언어로
    )
    text, _ = ask_text("text", prompt)   # 글만: '글 작업' 모델
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    songs = [s for s in data.get("songs", []) if s in set(names)][:2]
    return songs, str(data.get("reason", "")).strip()


# ---------- 본 작업 ----------

def common_name(names: list[str]) -> str:
    """폴더 이름들의 공통 이름('여행사진1','여행사진2' → '여행사진'). 없으면 '폴더1 · 폴더2'."""
    pre = names[0]
    for n in names[1:]:
        while pre and not n.startswith(pre):
            pre = pre[:-1]
    pre = pre.rstrip(" _-0123456789")
    return pre if len(pre) >= 2 else " · ".join(names)


def merge_scenes(per_folder: list[list[dict]]) -> list[dict]:
    """여러 폴더의 장면을 찍은 시각(sc['time'], 없으면 맨 뒤) 순서로 잇는다. 같으면 폴더 순서, 장면 순서."""
    flat = [(sc.get("time") or datetime.max, fi, si, sc) for fi, scs in enumerate(per_folder) for si, sc in enumerate(scs)]
    flat.sort(key=lambda x: x[:3])
    return [x[3] for x in flat]


def est_secs(chunk: str) -> float:
    return len(chunk) / 5.0 + GAP


def fit_scenes(scenes: list[dict], max_secs: float) -> list[dict]:
    """합친 영상이 max_secs 안쪽이 되게 장면마다 사진 수·이야기 토막을 줄이고(앞쪽만 남김),
    그래도 길면 사진이 적은 장면부터 뺀다. 처음 장면 순서는 그대로. 원본은 바꾸지 않는다."""
    def est(scs: list[dict]) -> float:
        return sum(max(LEAD + est_secs(sc["title"]) + sum(est_secs(c) for c in sc["chunks"]) + TAIL, len(sc["photos"]) * 3.0)
                   for sc in scs)

    def pick(photos: list[dict], n: int) -> list[dict]:
        if len(photos) <= n:
            return photos
        return [photos[int(i * len(photos) / n)] for i in range(n)]

    out = scenes
    for chunk_cap, photo_cap in [(99, 12), (4, 8), (3, 6), (2, 5), (2, 4), (1, 3)]:
        out = [dict(sc, chunks=sc["chunks"][:chunk_cap], photos=pick(sc["photos"], photo_cap)) for sc in scenes]
        if est(out) <= max_secs:
            return out
    while len(out) > 1 and est(out) > max_secs:
        drop = min(range(len(out)), key=lambda i: (len(out[i]["photos"]), -i))
        out = out[:drop] + out[drop + 1:]
    return out


def build_scenes(analysis: dict, with_time: bool = False) -> list[dict]:
    """analysis.json 에서 장면별 사진(경로·회전)·제목·이야기 토막을 뽑는다. with_time 이면 첫 사진 찍은 시각을 sc['time'] 에 둔다."""
    folder_path = Path(analysis.get("folder_path", ""))
    rotations = analysis.get("rotations", {})
    scenes = []
    for sc in analysis.get("scenes", []):
        photos = []
        for a in sc.get("analyses", []):
            fn = a.get("filename", "")
            p = folder_path / fn
            if fn and p.exists():
                photos.append({"path": p, "rotate": rotations.get(fn, a.get("rotate", 0))})
        story = sc.get("story", {}) or {}
        if not photos:
            continue
        item = {"title": story.get("title", T("scene_n", n=sc.get("scene_idx", ""))),
                "chunks": caption_chunks(story.get("text", "")), "photos": photos}
        if with_time:
            from scanner import get_photo_timestamp
            ts = get_photo_timestamp(photos[0]["path"])
            item["time"] = ts if ts != datetime.min else None
        scenes.append(item)
    return scenes


def make_video(folder_name: str, analysis: dict | None, cfg: dict, report, scenes: list[dict] | None = None,
               lo: float = 120.0, hi: float = 180.0) -> dict:
    """영상을 만들고 {path, seconds, size, photos, voice, music, reason, note} 를 돌려준다. report(단계, 이름, 진행) 로 진행을 알린다.
    여러 폴더면 scenes(합친 장면)와 lo/hi(전체 길이 목표 초)를 함께 넘긴다."""
    scenes = scenes if scenes is not None else build_scenes(analysis)
    if not scenes:
        raise RuntimeError(T("no_video_photos"))
    MYDATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="video_", dir=str(TOOLS_DATA)))
    try:
        # 1. 내레이션: 장면마다 [제목, 이야기 토막들]
        report(1, T("v_step_narr"), T("v_prog_narr"))
        texts = [t for sc in scenes for t in [sc["title"], *sc["chunks"]]]
        files, voice = synthesize(texts, tmp)
        durs = [media_duration(f) for f in files]
        k = 0
        for sc in scenes:
            n = 1 + len(sc["chunks"])
            sc["audio"], sc["secs"] = files[k:k + n], durs[k:k + n]
            k += n
        min_photo = choose_min_photo_sec([(sc["secs"], len(sc["photos"])) for sc in scenes], lo, hi)

        # 2. 장면마다 영상(사진 움직임 + 자막)과 내레이션 소리
        scene_videos, scene_audios, total = [], [], 0.0
        for si, sc in enumerate(scenes):
            report(2, T("v_step_scenes"), T("v_prog_scene", i=si + 1, n=len(scenes)))
            length = scene_length(sc["secs"], len(sc["photos"]), min_photo)
            sc["length"] = length
            total += length
            per = length / len(sc["photos"])
            frames = max(2, round(per * FPS))
            clips = []
            for pi, ph in enumerate(sc["photos"]):
                canvas = tmp / f"c{si}_{pi}.jpg"
                make_canvas(ph["path"], ph["rotate"], canvas)
                clip = tmp / f"v{si}_{pi}.mp4"
                run_ffmpeg(["-i", canvas.name, "-vf", zoom_filter(pi, frames), "-frames:v", str(frames), "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "22", "-an", clip.name], cwd=tmp)
                clips.append(clip)
                canvas.unlink()
            (tmp / f"l{si}.txt").write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="ascii")
            plain = tmp / f"p{si}.mp4"
            run_ffmpeg(["-f", "concat", "-i", f"l{si}.txt", "-c", "copy", plain.name], cwd=tmp)

            # 자막 시각: 제목 소리 동안 제목, 이후 토막마다 자막
            overlays, t = [], LEAD
            title_png = tmp / f"t{si}.png"
            make_title_png(sc["title"], title_png)
            overlays.append((title_png, 0.0, max(3.5, t + sc["secs"][0] + GAP + 0.3)))
            t += sc["secs"][0] + GAP
            for ci, chunk in enumerate(sc["chunks"]):
                png = tmp / f"s{si}_{ci}.png"
                make_caption_png(chunk, png)
                overlays.append((png, t, t + sc["secs"][1 + ci] + GAP))
                t += sc["secs"][1 + ci] + GAP
            inputs = ["-i", plain.name]
            chain, last = [], "0:v"
            for oi, (png, a, b) in enumerate(overlays):
                inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.2f}", "-i", png.name]
                chain.append(f"[{last}][{oi + 1}:v]overlay=enable='between(t,{a:.2f},{b:.2f})':shortest=1[o{oi}]")
                last = f"o{oi}"
            sv = tmp / f"s{si}.mp4"
            run_ffmpeg([*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-t", f"{length:.2f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-an", sv.name], cwd=tmp)
            scene_videos.append(sv)

            # 내레이션: 앞 쉼 + 토막마다 (소리 + 쉼) + 끝까지 무음
            ain, af = [], []
            for ai, a in enumerate(sc["audio"]):
                ain += ["-i", a.name]
                af.append(f"[{ai}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={GAP}[a{ai}]")
            labels = "".join(f"[a{i}]" for i in range(len(sc["audio"])))
            fc = ";".join(af) + f";{labels}concat=n={len(sc['audio'])}:v=0:a=1,adelay={int(LEAD * 1000)},apad=whole_dur={length:.2f}[out]"
            sa = tmp / f"a{si}.wav"
            run_ffmpeg([*ain, "-filter_complex", fc, "-map", "[out]", "-t", f"{length:.2f}", "-ar", "44100", "-ac", "1", sa.name], cwd=tmp)
            scene_audios.append(sa)

        # 3. 합치기 + 배경음악
        report(3, T("v_step_merge"), T("v_prog_merge"))
        (tmp / "all_v.txt").write_text("".join(f"file '{v.name}'\n" for v in scene_videos), encoding="ascii")
        (tmp / "all_a.txt").write_text("".join(f"file '{a.name}'\n" for a in scene_audios), encoding="ascii")
        run_ffmpeg(["-f", "concat", "-i", "all_v.txt", "-c", "copy", "video.mp4"], cwd=tmp)
        run_ffmpeg(["-f", "concat", "-i", "all_a.txt", "-c", "copy", "narr.wav"], cwd=tmp)

        music_dir = str(cfg.get("music_folder", "") or "")   # 도구 설정 > 배경음악 폴더
        songs, reason, music_note = [], "", ""
        if music_dir:
            try:
                songs, reason = pick_music(folder_name, scenes, list_music(music_dir), cfg)
                if not songs:
                    music_note = T("music_not_picked")
            except Exception as e:
                music_note = T("music_failed", err=e)
        out = MYDATA_DIR / f"{datetime.now():%Y%m%d-%H%M}_{folder_name}.mp4"
        total_s = f"{total:.2f}"
        if songs:
            ins = ["-i", "video.mp4", "-i", "narr.wav"]
            lens, paths = 0.0, []
            while lens < total + 4 and len(paths) < 8:   # 곡이 짧으면 목록을 되풀이
                for s in songs:
                    paths.append(Path(music_dir) / s)
                    lens += media_duration(paths[-1])
            for p in paths:
                ins += ["-i", str(p)]
            n = len(paths)
            if n == 1:
                mix = "[2:a]anull[m]"
            else:
                steps, prev = [], "2:a"
                for i in range(1, n):
                    steps.append(f"[{prev}][{i + 2}:a]acrossfade=d=3[x{i}]")
                    prev = f"x{i}"
                mix = ";".join(steps) + f";[{prev}]anull[m]"
            fc = (f"{mix};[m]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{total_s},volume=0.35,"
                  f"afade=t=in:d=2,afade=t=out:st={max(total - 4, 0):.2f}:d=4[mus];"
                  "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[n1][n2];"
                  "[mus][n1]sidechaincompress=threshold=0.03:ratio=8:attack=30:release=500[duck];"
                  f"[duck][n2]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                  f"afade=t=out:st={max(total - 4, 0):.2f}:d=4[aud]")
            run_ffmpeg([*ins, "-filter_complex", fc, "-map", "0:v", "-map", "[aud]", "-t", total_s, "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", str(out)], cwd=tmp)
        else:
            run_ffmpeg(["-i", "video.mp4", "-i", "narr.wav", "-map", "0:v", "-map", "1:a", "-t", total_s, "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", str(out)], cwd=tmp)
        return {"path": out, "seconds": media_duration(out), "size": out.stat().st_size,
                "photos": sum(len(s["photos"]) for s in scenes), "scenes": len(scenes), "voice": voice,
                "music": songs, "reason": reason, "music_note": music_note}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def format_video_card(folder_name: str, info: dict) -> str:
    from reporter import path_link
    mb = info["size"] / 1048576
    if info["music"]:
        music = " + ".join(Path(s).stem for s in info["music"])
    else:
        music = T("none_with", note=info["music_note"]) if info["music_note"] else T("none_word")
    reason = f" ({info['reason']})" if info["reason"] and info["music"] else ""
    return T("video_card", name=folder_name, link=path_link(Path(info["path"]).resolve()), photos=info["photos"], scenes=info["scenes"],
             length=clock(info["seconds"]), mb=f"{mb:.1f}", voice=info["voice"], music=music, reason=reason)


def run_video_worker(folder_name: str) -> None:
    """분리 프로세스 진입점: 영상을 만들고 끝나면 앱에 알린다(sdk.data.notify). 잠금·status 는 이야기책 작업과 같은 파일."""
    import os
    from worker import LOCK_FILE, update_status

    cfg = load_config()
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    names = [n for n in folder_name.split("+") if n]   # 여러 폴더는 '여행사진1+여행사진2'
    analyses = [json.loads((TOOLS_DATA / n / "analysis.json").read_text(encoding="utf-8")) for n in names]
    analysis = analyses[0]
    folder_path = Path(analysis.get("folder_path", ""))
    shown = " · ".join(names)
    TOOLS_DATA.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "folder": folder_name, "folder_path": str(folder_path),
                                     "started_at": started, "kind": "video"}, ensure_ascii=False), encoding="utf-8")
    try:
        def report(step: int, name: str, progress: str) -> None:
            update_status(folder_name, folder_path, "running", step, 3, T("video_status", step=name), progress, started)

        report(1, T("step_prepare"), "")
        if len(names) == 1:
            info = make_video(folder_name, analysis, cfg, report)
        else:
            report(1, T("v_step_join"), T("v_prog_join"))
            scenes = fit_scenes(merge_scenes([build_scenes(a, with_time=True) for a in analyses]), MULTI_HI - 30)
            if scenes:
                scenes[0]["title"] = f"{common_name(names)}, {scenes[0]['title']}"
            info = make_video(folder_name, None, cfg, report, scenes=scenes, lo=MULTI_LO, hi=MULTI_HI)
        card = format_video_card(shown, info)
        data.notify(card, telegram=bool(cfg.get("telegram", True)))
        update_status(folder_name, folder_path, "completed", 3, 3, T("step_done"), T("prog_video_len", length=clock(info["seconds"])), started)
    except Exception as e:
        traceback.print_exc()
        data.notify(T("video_failed", name=folder_name, err=e), telegram=bool(cfg.get("telegram", True)))
        update_status(folder_name, folder_path, "failed", 0, 3, T("step_failed"), str(e), started)
    finally:
        try:   # 내 잠금일 때만 지운다(다른 사진 작업의 잠금을 지우지 않게)
            if json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                LOCK_FILE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_video_worker(sys.argv[1])
