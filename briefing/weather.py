"""브리핑의 날씨: 장소 찾기와 두 가지 출처를 같은 모양으로.

- 장소: 도구 설정 '장소'. "이름 37.51,127.07"처럼 좌표가 있으면 그것, 없으면 Open-Meteo 지오코딩으로 찾아 data/tools/briefing/place.json 에 둔다.
- 출처: config/secrets.toml [kma] service_key 가 있고 장소가 한국이면 기상청 단기예보(확인알림 도구의 코드를 복사),
  아니면 Open-Meteo(키 없음, 비상업 무료, 전 세계).
돌려주는 모양: {"source", "place", "obs_time", "now": {"temp", "rain", "humidity", "wind"}, "days": {date: {"low", "high", "pop", "wet"}}}
  wet = 비·눈 예보가 있는 시(시각 글 목록).
"""
import json
import math
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import quote, unquote, urlencode

from common import Fail, T  # common 이 sdk 경로를 넣는다
from sdk import config, data  # noqa: E402

PLACE_CACHE = data.tool_dir("briefing") / "place.json"
COORD = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/"
SHORT_BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
AGENT = {"User-Agent": "ChukChukAI-briefing"}


def _get_json(url: str, timeout: float = 12):
    request = urllib.request.Request(url, headers=AGENT)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read(3_000_000).decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            status = getattr(exc, "code", None)
            if attempt == 0 and status in (None, 408, 429, 500, 502, 503, 504) and not isinstance(exc, ValueError):
                time.sleep(1)
                continue
            raise Fail(T("weather_unreachable")) from None
    raise Fail(T("weather_unreachable"))


# ---- 장소 ----
def resolve_place(text: str, language: str = "ko"):
    """(보이는 이름, 위도, 경도). 좌표가 있으면 그것, 없으면 지오코딩(같은 글이면 저장한 값)."""
    text = " ".join(str(text or "").split())
    if not text:
        raise Fail(T("place_missing"))
    if m := COORD.search(text):
        return (COORD.sub("", text).strip() or f"{m[1]},{m[2]}"), float(m[1]), float(m[2])
    try:
        saved = json.loads(PLACE_CACHE.read_text(encoding="utf-8"))
        if saved.get("query") == text:
            return saved["name"], float(saved["lat"]), float(saved["lon"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    words = text.split()
    found = None
    for query in [text] + words[::-1]:   # 전체 이름, 그다음 가장 작은 단위부터
        url = (f"https://geocoding-api.open-meteo.com/v1/search?name={quote(query)}&count=10&format=json"
               f"&language={'ko' if language == 'ko' else 'en'}")
        results = _get_json(url).get("results") or []
        if results:
            others = [w for w in words if w != query]

            def score(r):
                where = " ".join(str(r.get(k) or "") for k in ("name", "admin1", "admin2", "admin3", "admin4"))
                return sum(1 for w in others if w[:2] and w[:2] in where)
            found = max(results, key=score)   # 같으면 앞(인구가 많은 곳)
            break
    if not found:
        raise Fail(T("place_not_found", place=text))
    name = text if len(words) > 1 else ", ".join(x for x in (found.get("name"), found.get("admin1")) if x)
    PLACE_CACHE.write_text(json.dumps({"query": text, "name": name, "lat": found["latitude"], "lon": found["longitude"]},
                                      ensure_ascii=False), encoding="utf-8")
    return name, float(found["latitude"]), float(found["longitude"])


# ---- 기상청(확인알림 도구의 KMAClient·격자 변환을 복사) ----
def in_korea(lat: float, lon: float) -> bool:
    return 32.5 <= lat <= 39.0 and 124.0 <= lon <= 132.0


def kma_key() -> str:
    return str(config.secret("kma").get("service_key", "")).strip()


def to_grid(latitude: float, longitude: float):
    """위도·경도 → 기상청 5km 격자 번호(nx, ny). 기상청이 공개한 람베르트 정각원추도법 변환."""
    re_, grid = 6371.00877, 5.0
    slat1, slat2, olon, olat = (math.radians(v) for v in (30.0, 60.0, 126.0, 38.0))
    xo, yo = 43, 136
    re_ /= grid
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi / 4 + slat2 / 2) / math.tan(math.pi / 4 + slat1 / 2))
    sf = math.tan(math.pi / 4 + slat1 / 2) ** sn * math.cos(slat1) / sn
    ro = re_ * sf / math.tan(math.pi / 4 + olat / 2) ** sn
    ra = re_ * sf / math.tan(math.pi / 4 + math.radians(latitude) / 2) ** sn
    theta = math.radians(longitude) - olon
    if theta > math.pi:
        theta -= 2 * math.pi
    if theta < -math.pi:
        theta += 2 * math.pi
    theta *= sn
    return math.floor(ra * math.sin(theta) + xo + 0.5), math.floor(ro - ra * math.cos(theta) + yo + 0.5)


def _kma_items(key: str, operation: str, base: datetime, nx: int, ny: int):
    params = {"serviceKey": unquote(key) if "%" in key else key, "pageNo": 1, "numOfRows": 1000, "dataType": "JSON",
              "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"), "nx": nx, "ny": ny}
    request = urllib.request.Request(f"{KMA_URL}{operation}?{urlencode(params)}", headers=AGENT)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Fail(T("kma_unreachable")) from None   # 주소(키 포함)는 보이지 않는다
    try:
        body = json.loads(raw)
    except ValueError:
        raise Fail(T("kma_bad_key") if "SERVICE_KEY" in raw else T("kma_bad_format")) from None
    try:
        header = body["response"]["header"]
        if header["resultCode"] == "03":   # 자료 없음(아직 발표 전)
            return None
        if header["resultCode"] != "00":
            raise Fail(T("kma_error_code", code=header["resultCode"]))
        return body["response"]["body"]["items"]["item"] or None
    except (KeyError, TypeError):
        raise Fail(T("kma_bad_format")) from None


def _kma_latest(key, operation, bases, nx, ny):
    for base in bases:
        items = _kma_items(key, operation, base, nx, ny)
        if items:
            return base, items
    return None


def _short_bases(now: datetime, count: int = 4):
    bases, day = [], now.replace(minute=0, second=0, microsecond=0)
    while len(bases) < count:
        for hour in reversed(SHORT_BASE_HOURS):
            base = day.replace(hour=hour)
            if base <= now and len(bases) < count:
                bases.append(base)
        day = (day - timedelta(days=1)).replace(hour=23)
    return bases


def from_kma(key: str, name: str, lat: float, lon: float, now: datetime) -> dict:
    nx, ny = to_grid(lat, lon)
    hour = (now - timedelta(minutes=40)).replace(minute=0, second=0, microsecond=0)   # 관측은 정시 40분쯤 올라온다
    early = now.replace(hour=2, minute=0, second=0, microsecond=0) if now.hour * 60 + now.minute >= 130 else \
        (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)   # 오늘 하루치가 다 든 발표
    with ThreadPoolExecutor(3) as pool:   # 기상청 조회 세 번을 동시에
        jobs = [pool.submit(_kma_latest, key, "getUltraSrtNcst", [hour - timedelta(hours=i) for i in range(3)], nx, ny),
                pool.submit(_kma_latest, key, "getVilageFcst", _short_bases(now), nx, ny),
                pool.submit(_kma_items, key, "getVilageFcst", early, nx, ny)]
        obs, short, first = (j.result() for j in jobs)
    rows = {}
    for items in ((first or []), (short[1] if short else [])):   # 뒤의 것(최신)이 앞의 것을 덮는다
        for item in items:
            rows.setdefault((item["fcstDate"], item["fcstTime"]), {})[item["category"]] = str(item["fcstValue"])
    if not rows and not obs:
        raise Fail(T("weather_no_data"))
    o = {i["category"]: str(i["obsrValue"]) for i in obs[1]} if obs else {}
    pty_word = {"1": T("pty_rain"), "2": T("pty_rain_snow"), "3": T("pty_snow"), "4": T("pty_shower"), "5": T("pty_drizzle"),
                "6": T("pty_drizzle_snow"), "7": T("pty_snow_flurry")}
    current = None
    if "T1H" in o:
        pty = o.get("PTY", "0")
        current = {"temp": float(o["T1H"]), "rain": (pty_word.get(pty, T("pty_rain")) + (f" {o['RN1']}" if o.get("RN1") else ""))
                   if pty != "0" else "", "humidity": o.get("REH"), "wind": float(o["WSD"]) if o.get("WSD") else None}
    days = {}
    for i in range(4):
        day = now.date() + timedelta(days=i)
        values = {t: v for (d, t), v in rows.items() if d == day.strftime("%Y%m%d")}
        temps = [float(v["TMP"]) for v in values.values() if "TMP" in v]
        lows = [float(v["TMN"]) for v in values.values() if "TMN" in v] or temps
        highs = [float(v["TMX"]) for v in values.values() if "TMX" in v] or temps
        pops = [int(v["POP"]) for v in values.values() if "POP" in v]
        if lows and highs:
            days[day] = {"low": min(lows), "high": max(highs), "pop": max(pops) if pops else None,
                         "wet": [t[:2] for t, v in sorted(values.items()) if v.get("PTY", "0") != "0"]}
    return {"source": T("source_kma"), "place": name, "obs_time": obs[0] if obs else None, "now": current, "days": days}


# ---- Open-Meteo ----
def from_open_meteo(name: str, lat: float, lon: float, now: datetime) -> dict:
    params = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "auto", "forecast_days": 4, "wind_speed_unit": "ms",
              "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
              "hourly": "precipitation,weather_code",
              "daily": "temperature_2m_min,temperature_2m_max,precipitation_probability_max"}
    body = _get_json("https://api.open-meteo.com/v1/forecast?" + urlencode(params))
    try:
        cur, hourly, daily = body.get("current") or {}, body.get("hourly") or {}, body["daily"]
        current = None
        if cur.get("temperature_2m") is not None:
            code = int(cur.get("weather_code") or 0)
            current = {"temp": float(cur["temperature_2m"]), "rain": _wmo_rain(code) if code >= 51 or (cur.get("precipitation") or 0) > 0 else "",
                       "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m")}
        wet = {}
        for stamp, amount, code in zip(hourly.get("time", []), hourly.get("precipitation", []), hourly.get("weather_code", [])):
            if (amount or 0) > 0 or (code or 0) >= 51:
                wet.setdefault(stamp[:10], []).append(stamp[11:13])
        days = {}
        for i, stamp in enumerate(daily["time"]):
            low, high = daily["temperature_2m_min"][i], daily["temperature_2m_max"][i]
            if low is None or high is None:
                continue
            pop = (daily.get("precipitation_probability_max") or [None] * len(daily["time"]))[i]
            days[date.fromisoformat(stamp)] = {"low": float(low), "high": float(high), "pop": pop, "wet": wet.get(stamp, [])}
        obs_time = datetime.fromisoformat(cur["time"]) if cur.get("time") else None
    except (KeyError, TypeError, ValueError, IndexError):
        raise Fail(T("weather_no_data")) from None
    return {"source": "Open-Meteo", "place": name, "obs_time": obs_time, "now": current, "days": days}


def _wmo_rain(code: int) -> str:
    """WMO 날씨 코드 → 강수 말."""
    if code in (71, 73, 75, 77, 85, 86):
        return T("pty_snow")
    if code in (66, 67, 56, 57):
        return T("pty_rain_snow")
    if code in (80, 81, 82, 95, 96, 99):
        return T("pty_shower")
    if code in (51, 53, 55):
        return T("pty_drizzle")
    return T("pty_rain")


def load(place_text: str, now: datetime) -> dict:
    name, lat, lon = resolve_place(place_text, config.language())
    key = kma_key()
    if key and in_korea(lat, lon):
        return from_kma(key, name, lat, lon, now)
    return from_open_meteo(name, lat, lon, now)
