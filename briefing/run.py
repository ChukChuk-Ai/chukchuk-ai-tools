"""브리핑: 새 메일, 날씨·옷차림, 오늘·내일 할 일을 한 장의 카드로 모은다.

AI Jarvis 커밋 71a9229 의 tools/브리핑/run.py 를 옮겨, 개인 내용(정해 둔 동네, 실전거래)을 빼고 일반 브리핑으로 다시 썼다.
- 단락은 도구 설정(환경설정 > 도구 설정 > 브리핑)에서 켜고 끈다. 장소도 거기에 적는다.
- 세 자료(메일·날씨·할 일)를 동시에 읽는다. 한 단락이 실패하면 그 단락에만 "확인 못 함: 이유"를 쓰고 나머지는 계속한다.
- 메일 기준값은 카드를 다 보인 뒤에 저장한다(data/tools/briefing/mail_seen.json).
결과 양식: # 제목 → > 두세 문장 요약 → 단락별 ## 제목 → 요약 줄 → 표.
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from common import HERE, Fail, T  # common 이 sdk 경로를 넣는다
from sdk import config, data  # noqa: E402

import mailcheck  # noqa: E402
import weather  # noqa: E402

# (최저 기온 이하, 상태 글 키, 옷차림 글 키). 위에서부터 먼저 맞는 것을 쓴다.
COLD = [(0, "cold0", "wear0"), (5, "cold1", "wear1"), (10, "cold2", "wear2"), (16, "cold3", "wear3"),
        (22, "cold4", "wear4"), (27, "cold5", "wear5"), (99, "cold6", "wear6")]
WIDE_RANGE = 10   # 하루 기온 차이가 이만큼 이상이면 덧붙인다


def cell(text) -> str:
    return " ".join(str(text).replace("|", "/").split()) or "-"


def table(header, rows) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return "\n".join(out + ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows])


def deg(value) -> str:
    return f"{float(value):g}°C"


# ---- 단락: 새 메일 ----
def part_mail(found, facts):
    mails, total, save = found
    facts["mail"] = total
    lines = [T("mail_summary", n=total, recent=mailcheck.MAX_RECENT), ""]
    if not mails:
        return lines + [T("mail_none")], save
    return lines + [mailcheck.render(mails, total - len(mails), mailcheck.summarize(mails))], save


# ---- 단락: 날씨 ----
def part_weather(w, now, umbrella_pop, facts):
    stamp = f" · {w['obs_time']:%m-%d %H:%M}" if w.get("obs_time") else ""
    lines = [T("weather_summary", source=w["source"], place=w["place"], stamp=stamp), ""]
    rows = []
    cur = w.get("now")
    if cur:
        facts["temp"] = cur["temp"]
        bits = [deg(cur["temp"]), cur["rain"] or T("no_rain_now")]
        if cur.get("humidity") is not None:
            bits.append(T("humidity", value=cur["humidity"]))
        if cur.get("wind") is not None:
            bits.append(T("wind", value=f"{float(cur['wind']):g}"))
        rows.append([T("row_now"), " · ".join(bits)])
    else:
        rows.append([T("row_now"), T("not_checked", why=T("weather_now_missing"))])
    today = w["days"].get(now.date())
    if today:
        facts.update(low=today["low"], high=today["high"])
        pop = today["pop"]
        if today["wet"]:
            facts["wet"] = True
            first, last = today["wet"][0], today["wet"][-1]
            rain = (T("rain_hours", first=first, last=last) if first != last else T("rain_hour", hour=first)) +                 (T("pop_max", pop=pop) if pop is not None else "")
        else:
            rain = T("rain_none") + (T("pop_max_paren", pop=pop) if pop is not None else "")
        rows.append([T("row_today"), f"{deg(today['low'])} / {deg(today['high'])} · {rain}"])
        low, high = today["low"], today["high"]
        word_key, wear_key = next((c, wr) for limit, c, wr in COLD if low <= limit)
        facts["word"] = T(word_key)
        rainy = bool(today["wet"]) or (pop is not None and pop >= umbrella_pop)
        facts["umbrella"] = rainy
        text = T("clothes", low=deg(low), word=T(word_key), wear=T(wear_key))
        if high - low >= WIDE_RANGE:
            text += T("wide_range", diff=f"{high - low:g}")
        text += " · " + (T("umbrella_yes") + (T("pop_paren", pop=pop) if pop is not None else "") if rainy else T("umbrella_no"))
        rows.append([T("row_clothes"), text])
    else:
        rows.append([T("row_today"), T("not_checked", why=T("weather_today_missing"))])
    later = []
    for i in (1, 2, 3):
        day = now.date() + timedelta(days=i)
        d = w["days"].get(day)
        later.append(f"{day.month}/{day.day} " + (f"{d['low']:g}~{deg(d['high'])}" if d else T("no_data")))
    rows.append([T("row_days"), " · ".join(later)])
    return lines + [table([T("th_item"), T("th_detail")], rows)]


# ---- 단락: 오늘·내일 할 일(앱 '자동 실행 목록'의 다가오는 항목) ----
def _when(value: str):
    """(시각, 시각이 있는가). 날짜만이면 그날 0시."""
    text = str(value).strip()
    try:
        if len(text) <= 10:
            return datetime.combine(date.fromisoformat(text), datetime.min.time()), False
        return datetime.fromisoformat(text).replace(tzinfo=None), True
    except ValueError:
        return None


def part_upcoming(items, now, facts):
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    rows = []
    for item in items:
        start = _when(item["when"])
        if not start:
            continue
        when, timed = start
        end = _when(item["end"]) if item.get("end") else None
        last = end[0] if end else when
        if (end and not end[1]) or not timed:
            last = datetime.combine(last.date(), datetime.max.time())   # 종일은 그날 끝까지
        if last < now or when.date() > tomorrow:
            continue
        if when < now and last >= now and when.date() < today:
            label = T("ongoing", end=f"{last:%m/%d}")
        else:
            day = T("today") if when.date() <= today else T("tomorrow")
            label = f"{day} {when:%H:%M}" if timed else day
        detail = str(item.get("detail") or "").strip()
        rows.append([label, str(item.get("title") or "") + (f" ({detail})" if detail else "")])
    facts["todo"] = len(rows)
    lines = [T("upcoming_summary", n=len(rows)), ""]
    if not rows:
        return lines + [T("upcoming_none")]
    return lines + [table([T("th_when"), T("th_what")], rows)]


def summary_text(facts, place_short: str) -> str:
    """맨 위 두세 문장. 모델 없이 값으로 만든다."""
    out = []
    if "mail" in facts:
        out.append(T("top_mail_none") if not facts["mail"] else T("top_mail", n=facts["mail"]))
    if "temp" in facts or "low" in facts:
        text = T("top_weather_now", place=place_short, temp=deg(facts["temp"])) if "temp" in facts else place_short + ":"
        if "low" in facts:
            text += T("top_weather_day", low=f"{facts['low']:g}", high=deg(facts["high"]), word=facts.get("word", ""))
        wet = facts.get("wet") or facts.get("umbrella")
        out.append(text + (T("top_umbrella") if wet else T("top_dry")) if "low" in facts else text)
    if "todo" in facts:
        out.append(T("top_todo", n=facts["todo"]) if facts["todo"] else T("top_todo_none"))
    return " ".join(out)


def _try(fn, *args):
    try:
        return fn(*args), None
    except Fail as exc:
        return None, str(exc)
    except Exception as exc:   # 어떤 오류든 그 단락만 실패한다
        return None, f"{type(exc).__name__}: {str(exc)[:100]}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    cfg = config.tool_settings(HERE)
    now = datetime.now().replace(second=0, microsecond=0)
    wanted = [k for k in ("mail", "weather", "upcoming") if cfg.get(k, True)]
    if not wanted:
        print(T("all_off"), file=sys.stderr)
        sys.exit(1)
    loaders = {"mail": (mailcheck.fetch_new,), "weather": (weather.load, cfg.get("place", ""), now),
               "upcoming": (data.read_upcoming,)}
    with ThreadPoolExecutor(3) as pool:   # 차례로 읽으면 네트워크 기다림이 더해져 느리다
        jobs = {k: pool.submit(_try, *loaders[k]) for k in wanted}
        found = {k: job.result() for k, job in jobs.items()}

    facts, sections, commit, ok = {}, [], [], 0
    for key in wanted:
        value, error = found[key]
        if error is None:
            try:
                if key == "mail":
                    body, save = part_mail(value, facts)
                    commit.append(save)
                elif key == "weather":
                    body = part_weather(value, now, int(cfg.get("umbrella_pop", 60)), facts)
                else:
                    body = part_upcoming(value, now, facts)
                ok += 1
            except Fail as exc:
                error = str(exc)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:100]}"
        if error is not None:
            body = [T("not_checked", why=error)]
        sections.append((T(f"section_{key}"), body))
    if not ok:
        print(T("all_failed") + " " + " / ".join(" ".join(b) for _, b in sections), file=sys.stderr)
        sys.exit(1)
    place_short = (found.get("weather") or (None,))[0]
    place_short = place_short["place"] if place_short else ""
    lines = [T("title", date=f"{now:%m/%d}"), ""]
    top = summary_text(facts, place_short)
    if top:
        lines += ["> " + top, ""]   # "> " 문단은 앱이 대화창에도 보여 준다
    for title, body in sections:
        lines += [f"## {title}", ""] + body + [""]
    print("\n".join(lines).rstrip() + "\n")
    sys.stdout.flush()
    for save in commit:   # 출력까지 마친 뒤에 메일 기준값을 저장한다
        try:
            save()
        except OSError:
            pass


if __name__ == "__main__":
    main()
