"""걷기: 걷기 타이머를 켜고, 다녀와서 알려 준 걸음 수로 운동 시간·추정 거리·평균 속도·소모 칼로리를 계산해 기록한다.

AI Jarvis 커밋 763d6d2 의 tools/걷기/run.py 를 옮겼다. 원본과 다른 점:
- 체중·보폭은 도구 설정(환경설정 > 도구 설정 > 걷기)에서 정한다(원본의 "체중 65kg 설정" 말은 없앰).
- 시각은 이 PC 시간대(원본은 한국 시각 고정). 기록 CSV 의 칸 이름은 영문(엑셀로 열린다).
인자 JSON: {"text", "action": start|arrive|finish|status|history|cancel, "steps", "start_time", "end_time", "restart", "weight_kg", "stride_m"}
기록: data/mydata/walk/walk.csv(사용자 자료), 진행 중 상태: data/tools/walk/state.json
"""
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1]))   # sdk 찾기(앱 폴더는 앱이 알려 준다)
from sdk import config, data, text  # noqa: E402

T = text.load(HERE)
STATE_FILE = data.tool_dir("walk") / "state.json"
HISTORY_FILE = data.mydata("walk") / "walk.csv"
COLUMNS = ["number", "date", "start", "end", "minutes", "steps", "distance_km", "kcal", "speed_kmh", "weight_kg"]
NUMBER = r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"


class Fail(Exception):
    pass


def now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def parse_time(value) -> datetime:
    """ISO·'YYYY-MM-DD HH:MM(:SS)'·'HH:MM(:SS)'(오늘) → 이 PC 시각."""
    raw = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(raw.replace(" ", "T", 1) if re.match(r"\d{4}-", raw) else raw)
        return moment.astimezone()   # 시간대가 없으면 이 PC 시각으로 본다
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        return now().replace(hour=int(m[1]), minute=int(m[2]), second=int(m[3] or 0))
    raise Fail(T("bad_time", value=raw))


def body_settings(args) -> tuple[float, float]:
    """(체중 kg, 보폭 m). 캐릭터가 준 값이 먼저, 그다음 도구 설정."""
    cfg = config.tool_settings(HERE)
    try:
        weight = float(args.get("weight_kg") or cfg.get("weight_kg") or 70)
        stride = float(args.get("stride_m") or (float(cfg.get("stride_cm") or 70) / 100))
    except (TypeError, ValueError):
        raise Fail(T("bad_body")) from None
    if not (20 <= weight <= 250 and 0.3 <= stride <= 1.5):
        raise Fail(T("bad_body"))
    return weight, stride


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def duration_text(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, minutes, secs = total // 3600, total % 3600 // 60, total % 60
    return T("duration_h", h=hours, m=minutes, s=secs) if hours else T("duration_m", m=minutes, s=secs)


def pace(speed_kmh: float) -> tuple[float, str]:
    """(METs, 걸음 빠르기 말)."""
    for limit, mets, key in ((3.2, 2.5, "pace0"), (4.5, 3.0, "pace1"), (5.5, 3.8, "pace2"), (6.5, 4.5, "pace3")):
        if speed_kmh < limit:
            return mets, T(key)
    return 5.0, T("pace4")


def metrics(start: datetime, end: datetime, steps: int, weight: float, stride: float) -> dict:
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise Fail(T("end_before_start"))
    minutes, hours = seconds / 60, seconds / 3600
    distance = round(steps * stride / 1000, 2)
    speed = round(distance / hours, 1)
    mets, pace_word = pace(speed)
    by_time = mets * (3.5 * weight / 200) * minutes              # 시간 기반 METs 칼로리
    by_steps = steps * 0.04 * (weight / 70) * (mets / 3.0)        # 걸음 수 기반 보정
    return {"seconds": seconds, "minutes": round(minutes, 1), "duration": duration_text(seconds), "cadence": int(round(steps / minutes)),
            "distance": distance, "speed": speed, "pace": pace_word, "kcal": max(1, int(round((by_time + by_steps) / 2)))}


def read_history() -> list[dict]:
    try:
        with open(HISTORY_FILE, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def append_history(row: dict) -> str:
    last = max((int(m[1]) for r in read_history() if (m := re.fullmatch(r"W-(\d+)", str(r.get("number") or "")))), default=0)
    row = {"number": f"W-{last + 1}", **row}
    new_file = not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except OSError:
        raise Fail(T("write_failed", path=HISTORY_FILE)) from None   # 엑셀에서 열려 있으면 쓰지 못한다
    return row["number"]


def card(title, summary, head, rows, tail=()) -> str:
    lines = [f"# {title}", "", f"> {summary}", "", head, "", T("th_item_detail"), "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines + ([""] + list(tail) if tail else [])) + "\n"


def show(dt: datetime) -> str:
    return f"{dt:%Y-%m-%d %H:%M:%S}"


# ---- 동작 ----
def start(args, state):
    restart = bool(args.get("restart")) or any(w in args.get("text", "") for w in ("다시", "재시작", "restart"))
    if state.get("active") and not restart:
        began = parse_time(state["start"])
        elapsed = duration_text((now() - began).total_seconds())
        return card(T("running_title"), T("running_summary", elapsed=elapsed), T("running_head", start=show(began)),
                    [(T("row_start"), show(began)), (T("row_elapsed"), elapsed), (T("row_state"), T("state_running"))],
                    [T("restart_hint"), T("finish_hint")])
    began = parse_time(args["start_time"]) if args.get("start_time") else now()
    weight, stride = body_settings(args)
    kind = "evening_walk" if "저녁" in args.get("text", "") or "evening" in args.get("text", "").lower() else "walk"
    label = T(kind)
    save_state({"active": True, "start": began.isoformat(), "end": None, "label": kind})   # 글 키(보일 때 화면 언어로)
    return card(T("start_title", label=label), T("start_summary"), T("start_head", start=show(began), weight=f"{weight:g}", stride=f"{stride:.2f}"),
                [(T("row_start"), show(began)), (T("row_weight"), f"{weight:g} kg"), (T("row_stride"), f"{stride:.2f} m"),
                 (T("row_state"), T("state_measuring"))], [T("body_hint"), T("finish_hint")])


def need_active(state):
    if not state.get("active") or not state.get("start"):
        raise Fail(T("not_running"))
    return parse_time(state["start"])


def arrive(args, state):
    began = need_active(state)
    end = parse_time(args["end_time"]) if args.get("end_time") else now()
    state["end"] = end.isoformat()
    save_state(state)
    elapsed = duration_text((end - began).total_seconds())
    return card(T("arrive_title"), T("arrive_summary", end=f"{end:%H:%M:%S}", elapsed=elapsed),
                T("arrive_head", start=show(began), end=show(end), elapsed=elapsed),
                [(T("row_span"), f"{show(began)} ~ {show(end)}"), (T("row_total"), elapsed)], [T("finish_hint")])


def finish(args, state, steps):
    began = need_active(state)
    if not steps or steps <= 0:
        raise Fail(T("need_steps"))
    end = parse_time(args.get("end_time") or state.get("end")) if (args.get("end_time") or state.get("end")) else now()
    weight, stride = body_settings(args)
    m = metrics(began, end, steps, weight, stride)
    number = append_history({"date": f"{began:%Y-%m-%d}", "start": f"{began:%H:%M:%S}", "end": f"{end:%H:%M:%S}", "minutes": m["minutes"],
                             "steps": steps, "distance_km": f"{m['distance']:.2f}", "kcal": m["kcal"], "speed_kmh": f"{m['speed']:.1f}",
                             "weight_kg": f"{weight:g}"})
    label = T(state["label"]) if state.get("label") in ("walk", "evening_walk") else T("walk")
    save_state({"active": False, "last": number})
    return card(T("done_title", label=label), T("done_summary", label=label, duration=m["duration"], steps=f"{steps:,}", kcal=f"{m['kcal']:,}"),
                T("done_head", number=number, duration=m["duration"], steps=f"{steps:,}", km=f"{m['distance']:.2f}", kcal=f"{m['kcal']:,}",
                  weight=f"{weight:g}", stride=f"{stride:.2f}"),
                [(T("row_number"), f"[{number}]"), (T("row_span"), f"{show(began)} ~ {show(end)}"),
                 (T("row_total"), f"{m['duration']} ({T('minutes', n=m['minutes'])})"),
                 (T("row_steps"), T("steps_cadence", steps=f"{steps:,}", cadence=m["cadence"])),
                 (T("row_distance"), f"{m['distance']:.2f} km"), (T("row_speed"), f"{m['speed']:.1f} km/h ({m['pace']})"),
                 (T("row_kcal"), f"**{m['kcal']:,} kcal**")], [T("body_hint"), T("history_hint")])


def status(args, state):
    if not state.get("active") or not state.get("start"):
        weight, stride = body_settings(args)
        return card(T("status_title"), T("status_idle"), T("status_idle_head"),
                    [(T("row_state"), T("state_idle")), (T("row_body"), T("body_value", weight=f"{weight:g}", stride=f"{stride:.2f}"))],
                    [T("start_hint")])
    began = parse_time(state["start"])
    elapsed = duration_text((now() - began).total_seconds())
    return card(T("status_title"), T("running_summary", elapsed=elapsed), T("running_head", start=show(began)),
                [(T("row_start"), show(began)), (T("row_elapsed"), elapsed), (T("row_state"), T("state_running"))], [T("finish_hint")])


def history(args):
    records = list(reversed(read_history()))[:10]
    lines = [f"# {T('history_title')}", ""]
    if not records:
        return "\n".join(lines + [f"> {T('history_none')}", "", T("start_hint")]) + "\n"
    lines += [f"> {T('history_summary', n=len(records))}", "", T("history_head", n=len(records)), "", T("history_th"),
              "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        steps = str(r.get("steps") or "")
        lines.append(f"| [{r.get('number', '-')}] | {r.get('date', '-')} | {str(r.get('start', ''))[:5]}~{str(r.get('end', ''))[:5]} | "
                     f"{T('minutes', n=r.get('minutes', '-'))} | {f'{int(steps):,}' if steps.isdigit() else steps or '-'} | "
                     f"{r.get('distance_km', '-')} km | {r.get('speed_kmh', '-')} km/h | {r.get('kcal', '-')} kcal | {r.get('weight_kg', '-')} kg |")
    return "\n".join(lines + ["", T("file_note", path=HISTORY_FILE)]) + "\n"


def cancel(args, state):
    was = bool(state.get("active"))
    save_state({"active": False})
    return card(T("cancel_title"), T("cancel_done") if was else T("cancel_none"), T("cancel_head"),
                [(T("row_state"), T("state_cancelled"))], [T("start_hint")])


def steps_of(args, said: str):
    raw = args.get("steps")
    if raw not in (None, ""):
        digits = re.sub(r"[^\d]", "", str(raw))
        return int(digits) if digits else None
    for pattern in (NUMBER + r"\s*(?:보|걸음|steps?)", r"(?:끝|완료|도착|종료|done|finish(?:ed)?)\s*" + NUMBER, NUMBER + r"\s*(?:걸었어|했다)"):
        if m := re.search(pattern, said, re.IGNORECASE):
            return int(m[1].replace(",", ""))
    return int(said.replace(",", "")) if re.fullmatch(r"[0-9,]+", said) else None


def pick_action(said: str, steps, state) -> str:
    low = said.lower()
    if any(w in said for w in ("취소", "중단", "그만")) or "cancel" in low:
        return "cancel"
    if any(w in said for w in ("기록", "이력", "내역")) or "history" in low or "log" in low:
        return "history"
    if any(w in said for w in ("현황", "상태", "경과")) or "status" in low:
        return "status"
    if steps is None and (any(w in said for w in ("돌아왔", "다녀왔", "도착했")) or "back" in low):
        return "arrive"
    if steps is not None or any(w in said for w in ("끝", "완료", "걸었어")) or "done" in low or "finish" in low:
        return "finish"
    if any(w in said for w in ("시작", "타이머", "산책", "출발")) or "start" in low:
        return "start"
    return "status" if state.get("active") else "start"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except ValueError:
        args = {}
    args = args if isinstance(args, dict) else {}
    said = str(args.get("text") or "").strip()
    args["text"] = said
    state = load_state()
    try:
        steps = steps_of(args, said)
        action = args.get("action") or pick_action(said, steps, state)
        if action == "start":
            out = start(args, state)
        elif action == "arrive":
            out = arrive(args, state)
        elif action == "finish":
            out = finish(args, state, steps)
        elif action == "status":
            out = status(args, state)
        elif action == "history":
            out = history(args)
        elif action == "cancel":
            out = cancel(args, state)
        else:
            raise Fail(T("bad_action"))
    except Fail as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(out, end="")


if __name__ == "__main__":
    main()
