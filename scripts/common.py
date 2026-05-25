"""Shared helpers for health-data fetchers.

Conventions used across fetch_*.py:

- Every fetcher exposes a single ``fetch()`` function that returns a dict
  with the shape ``{"ok": bool, "data": <payload>, "error": <str|None>}``.
- Fetchers never raise; they always return a status dict. The aggregator
  decides whether to fall back to previous values.
- All times are PST (America/Los_Angeles) for display labels, ISO 8601
  with offset for machine-readable timestamps.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PST = ZoneInfo("America/Los_Angeles")

DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def now_pst() -> dt.datetime:
    return dt.datetime.now(PST)


def today_pst() -> dt.date:
    return now_pst().date()


def env(name: str, *, required: bool = False, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def ok(data: Any) -> dict:
    return {
        "ok": True,
        "data": data,
        "error": None,
        "fetched_at_iso": now_pst().isoformat(timespec="seconds"),
    }


def fail(err: Exception | str) -> dict:
    msg = str(err) if not isinstance(err, str) else err
    return {
        "ok": False,
        "data": None,
        "error": msg,
        "fetched_at_iso": now_pst().isoformat(timespec="seconds"),
    }


def week_window_sun_to_sat(today: dt.date | None = None) -> list[dt.date]:
    """Return list[date] for the Sun..Sat week that contains ``today``."""
    today = today or today_pst()
    sun_offset = (today.weekday() + 1) % 7
    sunday = today - dt.timedelta(days=sun_offset)
    return [sunday + dt.timedelta(days=i) for i in range(7)]


def trailing_7_days(today: dt.date | None = None) -> list[dt.date]:
    """Return list[date] for the 7 days ending on ``today``, inclusive.

    e.g. on a Monday this returns last Tue..today's Mon. The trailing window
    always has 7 days of recent activity regardless of where today sits in
    the calendar week, so charts don't look empty on Sunday/Monday mornings.
    """
    today = today or today_pst()
    return [today - dt.timedelta(days=6 - i) for i in range(7)]


def short_day_labels(dates: list[dt.date]) -> list[str]:
    """Return ['Tue', 'Wed', ...] for the given dates."""
    return [d.strftime("%a") for d in dates]


def minutes_to_label(mins: int | None) -> str:
    if mins is None:
        return "—"
    h, m = divmod(int(mins), 60)
    if h == 0:
        return f"{m} min"
    return f"{h} h {m} m"


def fmt_clock(t: dt.datetime | dt.time | None) -> str:
    if t is None:
        return "—"
    if isinstance(t, dt.datetime):
        t = t.timetz().replace(tzinfo=None)
    h = t.hour
    am = h < 12
    h12 = h % 12 or 12
    return f"{h12}:{t.minute:02d} {'AM' if am else 'PM'}"


def load_previous(path: Path) -> dict | None:
    """Read the previous data.json so we can preserve last-known-good values
    for any source that failed this run.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def merge_preserving_previous(new: dict, previous: dict | None, section: str) -> dict:
    """If the new payload for ``section`` is missing or marked stale, use the
    previous one. The aggregator calls this once per top-level section.
    """
    if previous and (section not in new or new[section] is None):
        if section in previous:
            return previous[section]
    return new.get(section)
