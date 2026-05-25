#!/usr/bin/env python3
"""
Zepp / Amazfit Health Data MCP Server
Accesses Eddie's Zepp health data directly via the Huami API.
Provides: steps, sleep, heart rate, stress, SpO2, PAI, weekly overviews.
"""

import base64
import json
import struct
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Zepp MCP needs pycryptodome for the encrypted-credential login flow. "
        "Install with: pip install pycryptodome"
    ) from e

# ── Config ──────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

# Regional API hosts — us2 for US accounts; swap to de2 / cn for other regions.
# As of the 2024+ Huami API rework, the canonical host is *.zepp.com (not .huami.com).
API_BASE      = "https://api-mifit-us2.zepp.com"   # data reads (band_data, stress, spo2, pai)
API_USER_BASE = "https://api-user-us2.zepp.com"    # encrypted credential exchange

# Public AES constants from the Zepp Android client (see huami-token by argrento on
# Codeberg). These are NOT secrets — they're hardcoded into the app and required by
# the server for the encrypted token exchange.
_ZEPP_AES_KEY = b"xeNtBVqzDc6tuNTh"
_ZEPP_AES_IV  = b"MAAAYAAAAAAAAABg"


def _zepp_encrypt(data: bytes) -> bytes:
    cipher = AES.new(_ZEPP_AES_KEY, AES.MODE_CBC, iv=_ZEPP_AES_IV)
    return cipher.encrypt(pad(data, AES.block_size))

mcp = FastMCP("Zepp Health")

# ── Auth (cached) ────────────────────────────────────────────────────────────
_cache: dict = {"token": None, "user_id": None, "expires_at": None}


def _get_token() -> tuple[str, str]:
    """Return (app_token, user_id).

    Auth precedence:
      1. In-memory cache (if not expired)
      2. Manually pasted app_token + user_id in config.json (skip server auth entirely)
      3. Live login via the encrypted-credential flow (email + password from config)

    The live flow has two steps:
      A. POST AES-encrypted credentials to api-user-us2.zepp.com/v2/registrations/tokens.
         Server replies with HTTP 303 — `access` and `refresh` tokens are in the
         Location header's query string.
      B. POST `code=<access_token>` to api-mifit-us2.zepp.com/v2/client/login.
         Response body has token_info.app_token and token_info.user_id.

    Reverse-engineered from huami-token v0.8.0 (codeberg.org/argrento/huami-token).
    The legacy `account.huami.com/v2/client/login` endpoint that the previous
    implementation used is effectively dead — it returns HTTP 400 0100 for all
    non-OAuth clients since the 2024 API rework.
    """
    now = datetime.now()
    if _cache["token"] and _cache["expires_at"] and now < _cache["expires_at"]:
        return _cache["token"], _cache["user_id"]

    # Always re-read config so credential/token changes take effect without restart
    cfg = _load_config()["zepp"]

    # Short-circuit: manually pasted token from browser/mitmproxy capture
    stored_token   = cfg.get("app_token", "").strip()
    stored_user_id = cfg.get("user_id", "").strip()
    if stored_token and stored_user_id:
        _cache.update(token=stored_token, user_id=stored_user_id,
                      expires_at=now + timedelta(days=85))
        return stored_token, stored_user_id

    email    = cfg["email"]
    password = cfg["password"]
    device_id = str(uuid.uuid4())

    # ── Step A ── encrypted credential exchange → access/refresh tokens ─────
    payload = {
        "emailOrPhone": email,
        "state":        "REDIRECTION",
        "client_id":    "HuaMi",
        "password":     password,
        "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
        "region":       "us-west-2",
        "token":        ["access", "refresh"],   # doseq=True → token=access&token=refresh
        "country_code": "US",
    }
    encoded   = urllib.parse.urlencode(payload, doseq=True).encode()
    encrypted = _zepp_encrypt(encoded)

    r1 = requests.post(
        f"{API_USER_BASE}/v2/registrations/tokens",
        data=encrypted,
        headers={
            "app_name":     "com.huami.midong",
            "appname":      "com.huami.midong",
            "cv":           "151689_9.12.5",
            "v":            "2.0",
            "appplatform":  "android_phone",
            "vb":           "202509151347",
            "vn":           "9.12.5",
            "user-agent":   "Zepp/9.12.5 (Pixel 4; Android 12; Density/2.75)",
            "x-hm-ekv":     "1",  # tells server payload is AES-encrypted
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "accept-encoding": "gzip",
        },
        allow_redirects=False,
        timeout=15,
    )
    if r1.status_code != 303:
        raise RuntimeError(
            f"Zepp credential exchange failed: expected HTTP 303 redirect, "
            f"got {r1.status_code}. Body: {r1.text[:200]}"
        )
    location = r1.headers.get("Location") or ""
    qs            = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    access_token  = (qs.get("access")  or [None])[0]
    refresh_token = (qs.get("refresh") or [None])[0]
    if not access_token:
        raise RuntimeError(
            f"Zepp auth: no access token in redirect Location. URL was: {location[:200]}"
        )

    # ── Step B ── access_token → app_token + user_id ────────────────────────
    r2 = requests.post(
        f"{API_BASE}/v2/client/login",
        data={
            "code":               access_token,
            "device_id":          device_id,
            "device_model":       "android_phone",
            "app_version":        "9.12.5",
            "dn":                 ("api-mifit.zepp.com,api-user.zepp.com,api-mifit.zepp.com,"
                                   "api-watch.zepp.com,app-analytics.zepp.com,auth.zepp.com,"
                                   "api-analytics.zepp.com"),
            "third_name":         "huami",
            "source":             "com.huami.watch.hmwatchmanager:9.12.5:151689",
            "app_name":           "com.huami.midong",
            "country_code":       "US",
            "grant_type":         "access_token",
            "allow_registration": "false",
            "lang":               "en",
            "countryState":       "US-NY",
        },
        headers={
            "app_name":        "com.huami.webapp",
            "appname":         "com.huami.webapp",
            "origin":          "https://user.zepp.com",
            "referer":         "https://user.zepp.com/",
            "user-agent":      "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "content-type":    "application/x-www-form-urlencoded; charset=UTF-8",
            "accept":          "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.5",
        },
        timeout=15,
    )
    if r2.status_code != 200:
        raise RuntimeError(
            f"Zepp login (step B) failed: HTTP {r2.status_code}. Body: {r2.text[:200]}"
        )
    info      = r2.json().get("token_info", {})
    app_token = info.get("app_token")
    user_id   = info.get("user_id")
    if not app_token or not user_id:
        raise RuntimeError(
            f"Zepp login (step B): missing app_token or user_id in response: {r2.text[:200]}"
        )

    _cache.update(token=app_token, user_id=str(user_id),
                  expires_at=now + timedelta(hours=12))
    return app_token, str(user_id)


def _band_data(date: str, query_type: str = "summary") -> list[dict]:
    """Fetch raw band_data entries for a given date and query_type."""
    token, uid = _get_token()
    resp = requests.get(
        f"{API_BASE}/v1/data/band_data.json",
        params=dict(query_type=query_type, device_type="0",
                    userid=uid, from_date=date, to_date=date),
        headers={"apptoken": token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _decode_summary(entry: dict) -> dict:
    """Base64-decode the summary blob and return a parsed dict."""
    raw = entry.get("summary", "")
    if not raw:
        return {}
    return json.loads(base64.b64decode(raw).decode())


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


# ── Tools ────────────────────────────────────────────────────────────────────

# Sleep blob (`slp`) key map — mapped from live API response 2026-04-27.
# Auto-discovered via debug_raw_band_data on a known-good day.
#   dp  = deep sleep minutes
#   lt  = light sleep minutes
#   dt  = REM sleep minutes  (NOT the legacy `rem` key — that's gone)
#   wk  = wake-during-sleep minutes
#   wc  = awakenings count
#   ss  = sleep score          (NOT legacy `sc`)
#   rhr = resting heart rate   (lives under slp, not stp)
#   st  = sleep start (Unix epoch seconds)
#   ed  = sleep end (Unix epoch seconds)
#   ebt = effective bed time minutes (includes naps)
#   obt = out-of-bed minutes (after final wake)
# Sanity check: dp + lt + dt + wk == (ed - st) / 60.

def _ts_to_iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


@mcp.tool()
def get_daily_summary(date: str = "") -> str:
    """
    Get Eddie's daily health summary for a given date: steps, distance,
    active calories, goal, and high-level sleep stats.
    Date format: YYYY-MM-DD. Defaults to today.
    """
    date = date or _today()
    rows = _band_data(date, "summary")
    if not rows:
        return f"No data available for {date}."

    out = []
    for entry in rows:
        s = _decode_summary(entry)
        stp = s.get("stp", {})
        slp = s.get("slp", {})
        deep, light, rem, wake = (slp.get("dp", 0), slp.get("lt", 0),
                                  slp.get("dt", 0), slp.get("wk", 0))
        out.append({
            "date":           entry.get("date_time", date),
            "steps":          stp.get("ttl", 0),
            "goal_steps":     s.get("goal", stp.get("goal", 8000)),
            "distance_m":     stp.get("dis", 0),
            "distance_km":    round(stp.get("dis", 0) / 1000, 2),
            "calories_active":stp.get("cal", 0),
            "resting_hr":     slp.get("rhr"),                # moved from stp → slp
            "sleep_total_min":deep + light + rem,            # tlt is gone; sum components
            "sleep_deep_min": deep,
            "sleep_light_min":light,
            "sleep_rem_min":  rem,
            "sleep_wake_min": wake,
            "sleep_score":    slp.get("ss"),                 # was sc, now ss
        })
    return json.dumps(out, indent=2)


@mcp.tool()
def get_sleep_detail(date: str = "") -> str:
    """
    Get Eddie's detailed sleep breakdown: deep, light, REM, wake time,
    sleep start/end times, and sleep score.
    Date format: YYYY-MM-DD. Defaults to yesterday (last night's sleep).
    """
    date = date or _yesterday()
    rows = _band_data(date, "summary")
    if not rows:
        return f"No sleep data available for {date}."

    out = []
    for entry in rows:
        slp = _decode_summary(entry).get("slp", {})
        if not slp:
            continue
        deep, light, rem, wake = (slp.get("dp", 0), slp.get("lt", 0),
                                  slp.get("dt", 0), slp.get("wk", 0))
        total_min = deep + light + rem  # actual sleep time, excluding wake
        out.append({
            "date":            date,
            "sleep_start":     _ts_to_iso(slp.get("st")),
            "sleep_end":       _ts_to_iso(slp.get("ed")),
            "total_min":       total_min,
            "total_hr":        round(total_min / 60, 2),
            "deep_min":        deep,
            "light_min":       light,
            "rem_min":         rem,                          # was slp.rem, now slp.dt
            "wake_min":        wake,
            "sleep_score":     slp.get("ss"),                # was sc, now ss
            "resting_hr":      slp.get("rhr"),
            "awakenings":      slp.get("wc"),
        })
    if not out:
        return f"No sleep data recorded for {date}."
    return json.dumps(out, indent=2)


def _decode_hr_blob(blob: bytes) -> tuple[list[tuple[int, int]], str]:
    """
    Try multiple known formats for the Huami `data_hr` binary blob and return
    (readings, format_name). Each reading is (minute_of_day, bpm).

    Format A — 1 byte per minute: raw[i] = bpm at minute i. 0/254/255 are
        sentinels for "no reading". This is the modern Amazfit format.
    Format B — 2 bytes per minute big-endian uint16: legacy format from
        early Mi Band firmware. Sentinels 0xfffe/0xffff.
    We pick whichever format yields the most plausible-looking readings
    (count of values in [30, 200] bpm).
    """
    candidates = []

    # Format A — 1 byte / minute
    a_readings: list[tuple[int, int]] = []
    for i in range(len(blob)):
        v = blob[i]
        if 30 <= v <= 200:
            a_readings.append((i, v))
    candidates.append(("1-byte", a_readings))

    # Format B — 2 bytes / minute big-endian
    b_readings: list[tuple[int, int]] = []
    for i in range(0, len(blob) - 1, 2):
        v = struct.unpack(">H", blob[i:i+2])[0]
        if 30 <= v <= 200:
            b_readings.append((i // 2, v))
    candidates.append(("2-byte-BE", b_readings))

    fmt, readings = max(candidates, key=lambda kv: len(kv[1]))
    return readings, fmt


@mcp.tool()
def get_heart_rate(date: str = "") -> str:
    """
    Get Eddie's heart rate data for a given date.
    Returns min, max, average, and an hourly breakdown.
    Date format: YYYY-MM-DD. Defaults to today.
    """
    date = date or _today()
    rows = _band_data(date, "detail")
    if not rows:
        return f"No heart rate data for {date}."

    all_readings: list[int] = []
    hourly: dict[int, list[int]] = {}
    blob_len = 0
    format_used = "n/a"

    for entry in rows:
        raw = entry.get("data_hr", "")
        if not raw:
            continue
        blob = base64.b64decode(raw)
        blob_len = max(blob_len, len(blob))
        readings, fmt = _decode_hr_blob(blob)
        format_used = fmt
        for minute, bpm in readings:
            all_readings.append(bpm)
            hourly.setdefault(minute // 60, []).append(bpm)

    if not all_readings:
        return json.dumps({
            "date":          date,
            "readings_count": 0,
            "blob_bytes":    blob_len,
            "note":          ("data_hr blob present but no plausible bpm "
                              "values decoded — format may have changed again. "
                              "Try a more recent date or extend _decode_hr_blob.") if blob_len
                             else "No data_hr blob in the API response for this date.",
        }, indent=2)

    hourly_avg = {
        f"{h:02d}:00": round(sum(vals) / len(vals), 1)
        for h, vals in sorted(hourly.items())
    }

    return json.dumps({
        "date":           date,
        "min_bpm":        min(all_readings),
        "max_bpm":        max(all_readings),
        "avg_bpm":        round(sum(all_readings) / len(all_readings), 1),
        "readings_count": len(all_readings),
        "blob_bytes":     blob_len,
        "format_used":    format_used,
        "hourly_avg_bpm": hourly_avg,
    }, indent=2)


# Stress / SpO2 / PAI / HRV are all stubbed below.
# As of the 2024+ Zepp/Huami API rework, these endpoints (`/users/{uid}/stress`,
# `/users/{uid}/spo2`, `/users/{uid}/pai`) return 404 on api-mifit-us2.zepp.com
# (and on every plausible relocation we tested: /v1/users/{uid}/X, /v1/data/X.json,
# /v1/data/X_data.json, etc.). The watch still measures these — they sync to the
# mobile Zepp app but are not exposed via the cloud REST API anymore. Best path
# forward for any of these: enable Apple Health export in the iOS Zepp app and
# read them from there.
_UNAVAILABLE_NOTE = (
    "This metric is not exposed by the current Zepp cloud API "
    "(endpoint moved/removed in the 2024+ API rework). The watch still "
    "records it, but to read it programmatically you need to enable "
    "Apple Health export in the iOS Zepp app and pull from HealthKit."
)


@mcp.tool()
def get_stress_data(date: str = "") -> str:
    """
    Stress level data for a given date.

    NOTE: Currently unavailable via the Zepp cloud API. See note in response.
    """
    return json.dumps({
        "date":      date or _today(),
        "available": False,
        "note":      _UNAVAILABLE_NOTE,
    }, indent=2)


@mcp.tool()
def get_spo2_data(date: str = "") -> str:
    """
    Blood oxygen (SpO2) readings for a given date.

    NOTE: Currently unavailable via the Zepp cloud API. See note in response.
    """
    return json.dumps({
        "date":      date or _today(),
        "available": False,
        "note":      _UNAVAILABLE_NOTE,
    }, indent=2)


@mcp.tool()
def get_pai_score(date: str = "") -> str:
    """
    PAI (Personal Activity Intelligence) — a weekly cardiovascular score.

    NOTE: Currently unavailable via the Zepp cloud API. See note in response.
    """
    return json.dumps({
        "date":      date or _today(),
        "available": False,
        "note":      _UNAVAILABLE_NOTE,
    }, indent=2)


@mcp.tool()
def get_hrv(date: str = "") -> str:
    """
    Heart rate variability (HRV) — the headline overnight recovery metric.

    NOTE: HRV is not exposed via the Zepp cloud API at all (we probed
    /v1/data/hrv*.json, /users/{uid}/hrv, /v1/users/{uid}/hrv, and band_data
    query_type=hrv — all 404 or returned the generic summary blob with no
    HRV field). To get HRV programmatically, enable Apple Health export
    in the iOS Zepp app and read it from HealthKit.
    """
    return json.dumps({
        "date":      date or _today(),
        "available": False,
        "note":      _UNAVAILABLE_NOTE,
    }, indent=2)


@mcp.tool()
def get_weekly_overview(days: int = 7) -> str:
    """
    Get Eddie's health overview for the past N days (default 7).
    Returns steps, sleep total, sleep score, and resting HR for each day.
    Useful for spotting trends and checking weekly averages.
    """
    results = []
    for i in range(days - 1, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rows = _band_data(date, "summary")
            if rows:
                s   = _decode_summary(rows[0])
                stp = s.get("stp", {})
                slp = s.get("slp", {})
                deep, light, rem = (slp.get("dp", 0), slp.get("lt", 0), slp.get("dt", 0))
                results.append({
                    "date":           date,
                    "steps":          stp.get("ttl", 0),
                    "distance_km":    round(stp.get("dis", 0) / 1000, 2),
                    "calories_active":stp.get("cal", 0),
                    "resting_hr":     slp.get("rhr"),                # moved from stp
                    "sleep_total_min":deep + light + rem,            # tlt is gone
                    "sleep_score":    slp.get("ss"),                 # was sc
                })
            else:
                results.append({"date": date, "note": "no data"})
        except Exception as e:
            results.append({"date": date, "error": str(e)})

    return json.dumps(results, indent=2)


@mcp.tool()
def get_workouts(limit: int = 10) -> str:
    """
    Recent workouts/runs/sessions captured by the watch.
    Returns track id, distance, calories, duration, average/max/min heart rate,
    and training effect for each session.
    """
    token, uid = _get_token()
    resp = requests.get(
        f"{API_BASE}/v1/sport/run/history.json",
        params={"source": "run.mifit.huami.com"},
        headers={"apptoken": token},
        timeout=15,
    )
    if resp.status_code != 200:
        return f"Failed to fetch workouts: HTTP {resp.status_code}"
    summary = resp.json().get("data", {}).get("summary", []) or []
    out = []
    for w in summary[:limit]:
        out.append({
            "trackid":           w.get("trackid"),
            "type":              w.get("type"),         # 223 = whatever sport mode
            "start":             _ts_to_iso(int(w["trackid"])) if w.get("trackid") else "",
            "end":               _ts_to_iso(int(w["end_time"])) if w.get("end_time") else "",
            "duration_sec":      int(w.get("run_time", 0) or 0),
            "duration_min":      round(int(w.get("run_time", 0) or 0) / 60, 1),
            "distance_m":        float(w.get("dis", 0) or 0),
            "calories":          float(w.get("calorie", 0) or 0),
            "avg_hr":            float(w.get("avg_heart_rate", 0) or 0),
            "max_hr":            int(w.get("max_heart_rate", 0) or 0),
            "min_hr":            int(w.get("min_heart_rate", 0) or 0),
            "training_effect":   w.get("te"),
        })
    return json.dumps(out, indent=2)


@mcp.tool()
def get_activity_data(date: str = "") -> str:
    """
    Get Eddie's hourly activity breakdown for a given date:
    steps per hour, calories, active minutes.
    Date format: YYYY-MM-DD. Defaults to today.
    """
    date  = date or _today()
    token, uid = _get_token()
    resp  = requests.get(
        f"{API_BASE}/v1/data/band_data.json",
        params=dict(query_type="detail", device_type="0",
                    userid=uid, from_date=date, to_date=date),
        headers={"apptoken": token},
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json().get("data", [])

    if not raw:
        return f"No activity data for {date}."

    parsed = []
    for entry in raw:
        s = _decode_summary(entry)
        parsed.append({
            "date":    date,
            "summary": s.get("stp", {}),
        })
    return json.dumps(parsed, indent=2)


if __name__ == "__main__":
    mcp.run()
