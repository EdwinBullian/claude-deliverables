"""Pull sleep + activity + weekly workouts from Zepp via Eddie's vendored MCP.

Strategy unchanged from before: import the vendored Zepp MCP and patch its
``_load_config`` to read GitHub-Secret env vars, so the GitHub Action reuses
the exact AES-encrypted *.zepp.com auth flow that already works locally.

Three fixes/additions in this version:
  1. ``today`` biometrics now fall back to the most-recent *synced* day.
     Zepp's cloud often has no data for the current partial day (the watch
     hasn't synced yet), which left RHR / steps / avg-HR blank on the widget.
     We now show the latest day that actually has data, labelled honestly.
  2. ``avg_hr`` is actually computed (decoded from the HR detail blob) instead
     of being hard-coded to None.
  3. ``workouts_week`` exposes every Zepp workout in the trailing-7-day window,
     classified (basketball / walking / training) with real duration + calories,
     bucketed per day. build_data.py merges this with the Notion lift log to
     drive the stacked strength chart (so basketball finally shows up).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# Make the vendored MCPs importable
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

import requests  # noqa: E402
import zepp_mcp as zm  # noqa: E402

from common import (  # noqa: E402
    PST,
    env,
    fail,
    fmt_clock,
    minutes_to_label,
    ok,
    short_day_labels,
    today_pst,
    trailing_7_days,
)


def _env_config() -> dict:
    """Stand-in for zepp_mcp._load_config that pulls from env vars."""
    return {
        "zepp": {
            "email":     env("ZEPP_EMAIL")     or "",
            "password":  env("ZEPP_PASSWORD")  or "",
            "app_token": env("ZEPP_APP_TOKEN") or "",
            "user_id":   env("ZEPP_USER_ID")   or "",
        }
    }


# Patch before anything in zm.* touches credentials
zm._load_config = _env_config


# --------------------------------------------------------------------------
# Workout type classification
# --------------------------------------------------------------------------
# Zepp logs activities as numeric type codes with no label. Eddie's codes
# (confirmed 2026-05-30): 52 = Basketball (long sessions), 223 = Walking,
# 85 = a generic "training" mode he uses for BOTH lifting and pickup hoops.
# Type 85 is therefore resolved against the Notion lift log in build_data.py
# (a same-day Notion lift claims one 85 session; the rest become basketball).
BASKETBALL_TYPES = {14, 52}
WALKING_TYPES = {6, 16, 223}
TRAINING_TYPES = {9, 50, 85}  # lift-or-ball, resolved downstream


def _classify(type_code) -> str:
    try:
        c = int(type_code)
    except (TypeError, ValueError):
        return "other"
    if c in BASKETBALL_TYPES:
        return "basketball"
    if c in WALKING_TYPES:
        return "walking"
    if c in TRAINING_TYPES:
        return "training"
    return "other"


def _date_label(d: dt.date) -> str:
    return d.strftime("%b %-d")


def _estimate_sleep_score(deep: int, light: int, rem: int, awakenings: int | None) -> int | None:
    total = deep + light + rem
    if total <= 0:
        return None
    deep_pct = deep / total * 100
    rem_pct = rem / total * 100
    duration_score = min(total / 480, 1.0) * 50
    deep_score = min(deep_pct / 15, 1.0) * 25
    rem_score = min(rem_pct / 25, 1.0) * 25
    awake_pen = min((awakenings or 0) * 2, 10)
    return int(round(duration_score + deep_score + rem_score - awake_pen))


def _summary_for(day: dt.date) -> dict:
    """Return the decoded summary dict for a given day, or {} on no-data."""
    try:
        rows = zm._band_data(day.isoformat(), "summary")
    except Exception:
        return {}
    if not rows:
        return {}
    return zm._decode_summary(rows[0]) or {}


def _avg_hr_for(day: dt.date) -> int | None:
    """Decode the HR detail blob for ``day`` and return the mean bpm, or None."""
    import base64
    try:
        rows = zm._band_data(day.isoformat(), "detail")
    except Exception:
        return None
    all_bpm: list[int] = []
    for entry in rows or []:
        raw = entry.get("data_hr", "")
        if not raw:
            continue
        try:
            readings, _fmt = zm._decode_hr_blob(base64.b64decode(raw))
        except Exception:
            continue
        all_bpm.extend(bpm for _minute, bpm in readings)
    if not all_bpm:
        return None
    return int(round(sum(all_bpm) / len(all_bpm)))


def _all_workouts() -> list[dict]:
    """Raw Zepp workout history (most recent first)."""
    try:
        token, _uid = zm._get_token()
        r = requests.get(
            f"{zm.API_BASE}/v1/sport/run/history.json",
            params={"source": "run.mifit.huami.com"},
            headers={"apptoken": token},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("data", {}).get("summary", []) or []
    except Exception:
        return []


def _workouts_week(week: list[dt.date], items: list[dict]) -> list[list[dict]]:
    """Bucket Zepp workouts into a per-day list over the trailing-7-day window.

    Returns a length-7 list; each element is a list of session dicts:
        {"cat": "basketball|walking|training|other", "type": <code>,
         "min": <float>, "cal": <int>}
    Tiny sessions (<2 min and <10 cal) are dropped as noise.
    """
    by_day: list[list[dict]] = [[] for _ in range(7)]
    idx_of = {d: i for i, d in enumerate(week)}
    for w in items:
        try:
            tid = int(w.get("trackid") or 0)
        except (TypeError, ValueError):
            continue
        if not tid:
            continue
        start = dt.datetime.fromtimestamp(tid, tz=PST).date()
        if start not in idx_of:
            continue
        minutes = round(int(w.get("run_time", 0) or 0) / 60, 1)
        cal = int(float(w.get("calorie", 0) or 0))
        if minutes < 2 and cal < 10:
            continue
        by_day[idx_of[start]].append({
            "cat": _classify(w.get("type")),
            "type": w.get("type"),
            "min": minutes,
            "cal": cal,
        })
    return by_day


def fetch() -> dict:
    """Return {sleep, activity, workouts_week} sections of the widget payload."""
    try:
        zm._get_token()
    except Exception as e:
        return fail(f"auth failed: {e}")

    try:
        today = today_pst()
        yesterday = today - dt.timedelta(days=1)
        week = trailing_7_days(today)  # today is always index 6

        # ---- Last night's sleep (stored under yesterday's date) ----
        slp = _summary_for(yesterday).get("slp", {}) or {}
        deep = slp.get("dp", 0)
        light = slp.get("lt", 0)
        rem = slp.get("dt", 0)
        wake = slp.get("wk", 0)
        total = deep + light + rem
        score = slp.get("ss") or _estimate_sleep_score(deep, light, rem, slp.get("wc"))
        bed_dt = dt.datetime.fromtimestamp(slp["st"], tz=PST) if slp.get("st") else None
        wake_dt = dt.datetime.fromtimestamp(slp["ed"], tz=PST) if slp.get("ed") else None

        sleep_payload = {
            "date_label": _date_label(yesterday) + " — Last Night",
            "score": score,
            "total_minutes": total or None,
            "total_label": minutes_to_label(total) if total else "—",
            "deep_minutes": deep,
            "light_minutes": light,
            "rem_minutes": rem,
            "awake_minutes": wake,
            "bedtime_label": fmt_clock(bed_dt),
            "wake_label": fmt_clock(wake_dt),
            "resting_hr": slp.get("rhr"),
            "awakenings": slp.get("wc"),
        }

        # ---- Per-day steps + RHR for the trailing-7-day chart ----
        steps_arr: list[int | None] = [None] * 7
        rhr_arr: list[int | None] = [None] * 7
        for i, day in enumerate(week):
            s = _summary_for(day)
            steps_arr[i] = (s.get("stp") or {}).get("ttl")
            rhr_arr[i] = (s.get("slp") or {}).get("rhr")

        # ---- "Today" biometrics: fall back to the most-recent synced day ----
        # Zepp cloud frequently has no data for the current partial day, which
        # used to blank the whole top row. Find the latest day WITH steps and
        # show that, labelled with its real date.
        snap_idx = next((i for i in range(6, -1, -1) if steps_arr[i] is not None), None)
        if snap_idx is None:
            snap_idx = 6
        snap_day = week[snap_idx]
        is_today = snap_day == today

        # RHR: prefer last night's sleep RHR (most current), else snapshot day's
        today_rhr = sleep_payload["resting_hr"] or rhr_arr[snap_idx]
        today_avg_hr = _avg_hr_for(snap_day)

        if is_today:
            activity_label = _date_label(today) + " — Today"
        else:
            activity_label = snap_day.strftime("%a %b %-d") + " — Latest"

        # ---- Weekly workouts (classified) ----
        workouts_raw = _all_workouts()
        by_day = _workouts_week(week, workouts_raw)

        # Activity strip = the longest of today's sessions, if any
        today_workout = None
        if by_day[6]:
            longest = max(by_day[6], key=lambda s: s["min"])
            today_workout = {
                "type": {"basketball": "Basketball", "walking": "Walk",
                         "training": "Training"}.get(longest["cat"], "Workout"),
                "duration_min": int(round(longest["min"])) or None,
                "calories": longest["cal"] or None,
                "avg_hr": None,
            }

        activity_payload = {
            "date_label": activity_label,
            "today": {
                "rhr": today_rhr,
                "steps": steps_arr[snap_idx],
                "avg_hr": today_avg_hr,
                "sleep_score": sleep_payload["score"],
            },
            "week": {
                "labels": short_day_labels(week),
                "steps": steps_arr,
                "rhr": rhr_arr,
                "today_index": 6,
            },
            "today_workout": today_workout,
        }

        workouts_week = {
            "labels": short_day_labels(week),
            "date_labels": [d.strftime("%a %-m/%-d") for d in week],
            "by_day": by_day,
        }

        return ok({
            "sleep": sleep_payload,
            "activity": activity_payload,
            "workouts_week": workouts_week,
        })
    except Exception as e:
        return fail(f"data pull failed: {e}")


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), indent=2, default=str))
