"""Pull sleep + activity from Zepp via Eddie's vendored Zepp MCP.

Strategy: import the MCP module as-is, then monkey-patch ``_load_config`` to
read credentials from environment variables (GitHub Secrets) instead of
``mcp/config.json``. This way the GitHub Action shares the exact same
hard-won auth flow (AES-encrypted credential exchange against the new
*.zepp.com endpoint) that's already working locally — no duplicate auth
code, no risk of drift.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

# Make the vendored MCPs importable
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

import zepp_mcp as zm  # noqa: E402

from common import (  # noqa: E402
    DAYS_SHORT,
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
    rows = zm._band_data(day.isoformat(), "summary")
    if not rows:
        return {}
    return zm._decode_summary(rows[0]) or {}


def _today_workout() -> dict | None:
    """Most recent workout that ended today (PST), or None."""
    token, _uid = zm._get_token()
    import requests
    r = requests.get(
        f"{zm.API_BASE}/v1/sport/run/history.json",
        params={"source": "run.mifit.huami.com"},
        headers={"apptoken": token},
        timeout=15,
    )
    if r.status_code != 200:
        return None
    items = (r.json() or {}).get("data", {}).get("summary", []) or []
    if not items:
        return None
    today = today_pst()
    today_start = int(dt.datetime.combine(today, dt.time(0, 0), tzinfo=PST).timestamp())
    today_end = today_start + 86400
    for w in items:
        try:
            end_ts = int(w.get("end_time") or w.get("trackid") or 0)
        except (TypeError, ValueError):
            continue
        if today_start <= end_ts < today_end:
            return {
                "type": _sport_type_label(w.get("type")),
                "duration_min": int(w.get("run_time", 0) or 0) // 60 or None,
                "calories": int(float(w.get("calorie", 0) or 0)),
                "avg_hr": int(float(w.get("avg_heart_rate", 0) or 0)) or None,
            }
    return None


def _sport_type_label(code) -> str:
    mapping = {
        1: "Run", 6: "Walk", 8: "Cycling", 9: "Strength",
        10: "Yoga", 12: "Elliptical", 14: "Basketball", 16: "Hike",
        50: "Strength", 52: "Yoga", 60: "Elliptical", 92: "HIIT",
        223: "Strength",
    }
    try:
        return mapping.get(int(code), "Workout")
    except (TypeError, ValueError):
        return "Workout"


def fetch() -> dict:
    """Return the {sleep, activity} sections of the widget payload."""
    try:
        zm._get_token()
    except Exception as e:
        return fail(f"auth failed: {e}")

    try:
        today = today_pst()
        yesterday = today - dt.timedelta(days=1)
        # Trailing 7-day window ending today, so the activity chart always
        # shows the most-recent week regardless of where today falls.
        week = trailing_7_days(today)

        # Last-night sleep is stored under yesterday's date
        slp = _summary_for(yesterday).get("slp", {}) or {}
        deep = slp.get("dp", 0)
        light = slp.get("lt", 0)
        rem = slp.get("dt", 0)
        wake = slp.get("wk", 0)
        total = deep + light + rem
        score = slp.get("ss") or _estimate_sleep_score(deep, light, rem, slp.get("wc"))
        bed_dt = (
            dt.datetime.fromtimestamp(slp["st"], tz=PST)
            if slp.get("st") else None
        )
        wake_dt = (
            dt.datetime.fromtimestamp(slp["ed"], tz=PST)
            if slp.get("ed") else None
        )

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

        # Per-day steps + RHR for the trailing-7-day chart. today is always
        # the last cell (index 6); no future-day handling needed.
        steps_arr: list[int | None] = [None] * 7
        rhr_arr: list[int | None] = [None] * 7
        today_idx = 6

        for i, day in enumerate(week):
            try:
                s = _summary_for(day)
                steps_arr[i] = (s.get("stp") or {}).get("ttl")
                rhr_arr[i] = (s.get("slp") or {}).get("rhr")
                time.sleep(0.3)
            except Exception:
                continue

        # Today's totals + workout
        today_summary = _summary_for(today)
        t_stp = today_summary.get("stp") or {}
        t_slp = today_summary.get("slp") or {}

        activity_payload = {
            "date_label": _date_label(today) + " — Today",
            "today": {
                "rhr": t_slp.get("rhr"),
                "steps": t_stp.get("ttl"),
                "avg_hr": None,
                "sleep_score": sleep_payload["score"],
            },
            "week": {
                "labels": short_day_labels(week),
                "steps": steps_arr,
                "rhr": rhr_arr,
                "today_index": today_idx,
            },
            "today_workout": _today_workout(),
        }

        return ok({"sleep": sleep_payload, "activity": activity_payload})
    except Exception as e:
        return fail(f"data pull failed: {e}")


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), indent=2, default=str))
