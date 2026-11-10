"""문서 도구: 마크다운으로 PDF·Word·PowerPoint 만들기, PDF 글 뽑기.

인자 JSON:
- 만들기: {"action": "make", "source": "마크다운 파일 경로" 또는 "markdown": "본문",
           "title": "제목(없으면 본문 첫 # 줄)", "formats": ["pdf", "docx", "pptx", "md", "txt"],
           "folder": "저장 폴더(없으면 data/mydata/reports)", "images": ["그림 경로", ...]}
- 읽기:   {"action": "read", "path": "PDF 경로", "start_page": 1, "end_page": 0}
명령 줄 길이(약 3만 자) 때문에 긴 본문은 파일로 써서 source 로 넘긴다.
"""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1]))   # sdk 찾기(앱 폴더는 앱이 알려 준다)
sys.path.insert(1, str(HERE))
from sdk import data, text as texts  # noqa: E402

import doclib  # noqa: E402

T = texts.load(HERE)
DEFAULT_FOLDER = data.mydata("reports")

NAMES = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "md": T("format_md"), "txt": T("format_txt")}


class Fail(Exception):
    pass


def make(args):
    if args.get("source"):
        source = Path(str(args["source"]))
        try:
            text = source.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise Fail(T("source_failed", source=source, reason=exc.strerror or exc)) from None
        base = source.parent
    else:
        text, base = str(args.get("markdown") or ""), None
    if not text.strip():
        raise Fail(T("no_body"))
    title = str(args.get("title") or "").strip()
    if not title:
        heading = re.search(r"^#\s+(.+)$", text, re.M)
        title = heading.group(1).strip() if heading else T("untitled")
    formats = args.get("formats") or ["pdf"]
    if isinstance(formats, str):   # 표준 칸은 글("pdf, docx"), 배열도 받는다
        formats = re.split(r"[,\s]+", formats.strip())
    folder = Path(str(args.get("folder") or DEFAULT_FOLDER))
    images = args.get("images") or doclib.local_images(text, base)
    try:
        result = doclib.export(text, title, folder, formats, images)
    except ValueError as exc:
        raise Fail(str(exc)) from None
    saved, failed = result["saved"], result["failed"]
    if not saved:
        raise Fail(T("make_failed", reasons="; ".join(failed)))
    lines = [T("make_title", title=doclib.plain(title)), "",
             T("make_summary", n=len(saved), folder=folder) + (T("make_summary_failed", n=len(failed)) if failed else ""), "",
             T("make_header"), "|---|---|"]
    for path in saved:
        p = Path(path)
        lines.append(f"| {NAMES.get(p.suffix[1:], p.suffix[1:])} | {p.name} |")
    if failed:
        lines += ["", T("failed_heading")] + [f"- {f}" for f in failed]
    if images:
        lines += ["", T("images_added", n=min(len(images), doclib.MAX_IMAGES))]
    return "\n".join(lines)


def read(args):
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        raise Fail(T("need_path"))
    try:
        start = int(args.get("start_page") or 0)
        end = int(args.get("end_page") or 0)
    except (TypeError, ValueError):
        raise Fail(T("bad_pages")) from None
    try:
        data = doclib.read_pdf(path, start, end)
    except doclib.ReadError as exc:
        raise Fail(str(exc)) from None
    lines = [T("read_title", name=data["name"]), "",
             T("read_summary", total=data["total"], first=data["first"], last=data["last"], chars=data["chars"]), "",
             doclib.UNTRUSTED_NOTE]
    lines += [f"- {note}" for note in data["notes"]]
    for number, text in data["pages"]:
        lines += ["", T("page_label", n=number), "", text or T("no_text")]
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except ValueError:
        args = {}
    action = args.get("action") or ("read" if args.get("path") else "make")
    try:
        if action == "make":
            print(make(args))
        elif action == "read":
            print(read(args))
        else:
            raise Fail(T("bad_action"))
    except Fail as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
