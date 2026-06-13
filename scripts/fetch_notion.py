"""Pull workout sessions + supplement stack from Notion.

Uses the official Notion API. Requires:
  NOTION_TOKEN              – integration secret (https://www.notion.so/profile/integrations)
  NOTION_WORKOUT_DB_ID      – workout tracker database id
  NOTION_SUPPLEMENT_DB_ID   – (optional) supplement stack database id; if unset,
                              the static fallback in the example data is preserved.

The workout-tracker query assumes these property names (case-insensitive):
  Date              (date)
  Type / Category   (select)    e.g. Push, Pull, Legs, Strength, Cardio, Basketball
  Duration / Min    (number)    minutes
  Top Lifts         (rich_text) optional; pipe-separated lines, e.g.
                                "Incline DB Press|55 lb × 8|→ try 60"

Property names are matched case-insensitively against the database schema
the first time we query it. If a field is missing we just leave the
corresponding output field empty rather than 500.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import requests

from common import (
    DAYS_SHORT,
    env,
    fail,
    ok,
    short_day_labels,
    today_pst,
    trailing_7_days,
    week_window_sun_to_sat,
)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {env('NOTION_TOKEN', required=True)}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _query_db(db_id: str, filter_payload: dict | None = None) -> list[dict]:
    results: list[dict] = []
    cursor = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if filter_payload:
            body["filter"] = filter_payload
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=_headers(),
            json=body,
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
    return results


def _prop(page: dict, *names: str) -> Any:
    """Case-insensitively pull a property from a Notion page; returns the
    inner ``value`` shape (rich_text / number / select / date)."""
    props = page.get("properties", {})
    norm = {k.lower(): v for k, v in props.items()}
    for n in names:
        v = norm.get(n.lower())
        if v is not None:
            return v
    return None


def _text(rich) -> str:
    if not rich or rich.get("type") != "rich_text":
        return ""
    return "".join(piece.get("plain_text", "") for piece in rich.get("rich_text", []))


def _num(node) -> float | None:
    if not node or node.get("type") != "number":
        return None
    return node.get("number")


def _select(node) -> str | None:
    if not node or node.get("type") != "select":
        return None
    sel = node.get("select")
    return sel.get("name") if sel else None


def _date(node) -> dt.date | None:
    if not node or node.get("type") != "date":
        return None
    d = node.get("date")
    if not d or not d.get("start"):
        return None
    return dt.date.fromisoformat(d["start"][:10])


_LIFT_WORDS = ("push", "pull", "upper", "lower", "legs", "arms", "chest",
               "back", "shoulders", "strength", "full body")
_LIFT_SPLIT_LABELS = {"push": "Push", "pull": "Pull", "upper": "Upper",
                      "lower": "Lower", "legs": "Legs"}


def _categorize(day_type: str | None, name: str | None) -> tuple[str, str]:
    """Classify a workout row into (category, lift_split).

    Eddie often leaves the 'Day Type' select EMPTY on basketball/walk entries —
    only the page title carries the activity (e.g. a page created from the
    'Basketball' template is named "Basketball"). So fall back to the title.
    """
    key = ((day_type or "").strip() or (name or "").strip()).lower()
    if not key or "rest" in key:
        return ("rest", "")
    if "basketball" in key or "hoops" in key:
        return ("basketball", "")
    if "walk" in key or "run" in key:
        return ("walking", "")
    for w in _LIFT_WORDS:
        if w in key:
            split = (day_type or "").strip() or _LIFT_SPLIT_LABELS.get(w, w.title())
            return ("lift", split)
    return ("other", "")


def _build_strength_week(workouts: list[dict]) -> dict:
    today = today_pst()
    # Trailing 7 days ending today, so the chart always shows recent activity
    # regardless of where today falls in the calendar week.
    week = trailing_7_days(today)
    iso_to_idx = {d.isoformat(): i for i, d in enumerate(week)}

    lift_type: list[str] = [""] * 7
    lift_min: list[int] = [0] * 7            # estimate; Zepp duration wins in merge
    notion_bball_min: list[float] = [0.0] * 7
    notion_walk_min: list[float] = [0.0] * 7
    most_recent_top_lifts_raw = ""

    # Per-type duration estimates (minutes). The "Workout Schedule" DB usually
    # doesn't log a session duration, so these stand in until Zepp supplies the
    # real number (the merge prefers Zepp). A Duration column, if present, wins.
    DURATION_ESTIMATES = {
        "Upper": 85, "Lower": 75, "Push": 80, "Pull": 80, "Legs": 70,
        "Basketball": 45, "Running": 35, "Walking": 45, "Rest": 0,
    }

    # Sort newest first so the latest session wins same-day ties
    workouts.sort(key=lambda w: (w.get("created_time") or ""), reverse=True)

    for page in workouts:
        d_node = _prop(page, "Date", "Workout Date", "Day")
        type_node = _prop(page, "Day Type", "Type", "Category", "Split")
        dur_node = _prop(page, "Duration", "Min", "Minutes", "Length")
        lifts_node = _prop(page, "Top Lifts", "Lifts", "Notes")

        d = _date(d_node)
        if not d or d.isoformat() not in iso_to_idx:
            continue
        idx = iso_to_idx[d.isoformat()]

        cat, split = _categorize(_select(type_node), _title(page))
        dur = _num(dur_node)

        if cat == "lift":
            if not lift_type[idx]:
                lift_type[idx] = split or "Strength"
            est = int(dur) if dur else DURATION_ESTIMATES.get(split, 60)
            lift_min[idx] = max(lift_min[idx], est)
            if not most_recent_top_lifts_raw:
                most_recent_top_lifts_raw = _text(lifts_node)
        elif cat == "basketball":
            notion_bball_min[idx] += float(dur) if dur else DURATION_ESTIMATES["Basketball"]
        elif cat == "walking":
            notion_walk_min[idx] += float(dur) if dur else DURATION_ESTIMATES["Walking"]
        # rest / other -> no chart segment

    top_lifts = _parse_top_lifts(most_recent_top_lifts_raw)

    # Dynamic labels: Mon/Tue/etc. based on the trailing window
    day_labels = short_day_labels(week)
    date_labels = [d.strftime("%a %-m/%-d") for d in week]

    return {
        "week_label": "Training — Last 7 Days",
        "labels": day_labels,
        "date_labels": date_labels,
        "lift_type": lift_type,
        "lift_min": lift_min,
        "notion_bball_min": notion_bball_min,
        "notion_walk_min": notion_walk_min,
        "top_lifts": top_lifts,
    }


def _parse_top_lifts(raw: str) -> list[dict]:
    """Accept pipe-separated lines:
       'Incline DB Press|55 lb × 8|→ try 60'
       'Overhead Press|35 lb × 12|→ try 40'
    """
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        parts = [p.strip() for p in re.split(r"\||\t", line) if p.strip()]
        if len(parts) < 2:
            continue
        out.append({
            "name": parts[0],
            "value": parts[1],
            "next": parts[2] if len(parts) >= 3 else "",
        })
    return out[:4]


def _build_supplements(supps: list[dict]) -> list[dict]:
    out = []
    for page in supps:
        out.append({
            "name": _text(_prop(page, "Name", "Title")) or _title(page),
            "desc": _text(_prop(page, "Description", "Notes", "Desc")) or "",
            "dose": _text(_prop(page, "Dose", "Dosage")) or "",
            "time": _text(_prop(page, "Timing", "Time", "When")) or "",
        })
    return [o for o in out if o["name"]]


def _title(page: dict) -> str:
    for v in page.get("properties", {}).values():
        if v.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in v.get("title", []))
    return ""


def fetch() -> dict:
    workout_db = env("NOTION_WORKOUT_DB_ID")
    supp_db = env("NOTION_SUPPLEMENT_DB_ID")
    if not workout_db and not supp_db:
        return fail("no NOTION_*_DB_ID env vars set")

    payload: dict[str, Any] = {}

    try:
        if workout_db:
            today = today_pst()
            window = trailing_7_days(today)
            # Filter by the Date property (not created_time) so workouts whose
            # session date was set independently of page creation are caught.
            # Property is named "Date" in the Workout Schedule DB.
            workouts = _query_db(workout_db, {
                "property": "Date",
                "date": {"on_or_after": window[0].isoformat()},
            })
            payload["strength_week"] = _build_strength_week(workouts)
    except Exception as e:
        return fail(f"workout pull failed: {e}")

    try:
        if supp_db:
            supps = _query_db(supp_db)
            payload["supplements"] = _build_supplements(supps)
    except Exception as e:
        # Don't fail the whole fetch on supplements
        payload.setdefault("supplements", None)

    return ok(payload)


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), indent=2, default=str))
