"""Aggregate Zepp + Cronometer + Notion into a single ``data.json`` payload.

Per-section refresh
-------------------
Different widgets want different freshness, so this script can refresh just a
subset of sections and preserve the rest from the previous ``data.json``:

    python build_data.py data.json                      # full refresh (default)
    python build_data.py data.json --sections fast      # steps + water only
    python build_data.py data.json --sections nutrition # macros/micros + water

``--sections`` accepts a named preset (full / fast / nutrition) or a literal
comma list of top-level sections (e.g. ``activity,hydration``). Only the sources
needed for the requested sections are contacted, so the frequent "fast" job
doesn't re-pull a full week of nutrition every time.

Merge
-----
The strength chart is a merge of two sources: the Notion lift log (what kind of
lift + top lifts) and the Zepp workout history (real durations + calories, plus
basketball/walking that Notion never sees). See ``_merge_strength``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fetch_cronometer
import fetch_notion
import fetch_zepp
from common import load_previous, now_pst

# Which top-level sections each source produces.
SOURCE_SECTIONS = {
    "zepp": ["sleep", "activity", "_workouts_week"],
    "cronometer": ["macros_week", "micros_week", "hydration"],
    "notion": ["_strength_notion", "supplements"],
}

# Final sections that appear in data.json (energy + strength_week are derived).
ALL_SECTIONS = [
    "sleep", "activity", "energy", "strength_week", "supplements",
    "macros_week", "micros_week", "hydration",
]

PRESETS = {
    "full": ALL_SECTIONS,
    "fast": ["activity", "hydration"],
    "nutrition": ["macros_week", "micros_week", "hydration"],
}

# A derived/merged section depends on these raw inputs being refreshed.
DERIVED_INPUTS = {
    "energy": ["sleep", "activity"],
    "strength_week": ["_strength_notion", "_workouts_week"],
}

LIFT_SPLITS = {"push", "pull", "legs", "upper", "lower", "full body", "arms",
               "chest", "back", "shoulders", "strength"}


# ---- Strength merge ------------------------------------------------------

def _merge_strength(notion: dict | None, zepp_workouts: dict | None) -> dict | None:
    """Combine the Notion lift log with classified Zepp workouts into a
    stacked-bar payload: per day, a Lift segment (Notion split + Zepp duration/
    calories), a Basketball segment, and a Walking segment.
    """
    if not notion and not zepp_workouts:
        return None

    labels = (notion or {}).get("labels") or (zepp_workouts or {}).get("labels") \
        or ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    date_labels = (notion or {}).get("date_labels") \
        or (zepp_workouts or {}).get("date_labels") or labels

    n_types = (notion or {}).get("types") or [""] * 7
    n_min = (notion or {}).get("minutes") or [0] * 7
    by_day = (zepp_workouts or {}).get("by_day") or [[] for _ in range(7)]

    lift_min = [0] * 7
    lift_cal = [0] * 7
    lift_type = [""] * 7
    bball_min = [0.0] * 7
    bball_cal = [0] * 7
    walk_min = [0.0] * 7
    walk_cal = [0] * 7

    for i in range(7):
        sessions = by_day[i] if i < len(by_day) else []
        training = [s for s in sessions if s["cat"] == "training"]
        bball = [s for s in sessions if s["cat"] == "basketball"]
        walk = [s for s in sessions if s["cat"] == "walking"]

        notion_lift = (n_types[i] or "").strip()
        has_notion_lift = notion_lift.lower() in LIFT_SPLITS or (
            notion_lift != "" and notion_lift.lower() not in
            {"basketball", "walking", "running", "rest", "cardio"}
        )

        if has_notion_lift:
            lift_type[i] = notion_lift
            if training:
                # The longest same-day "training" session IS the lift.
                training.sort(key=lambda s: s["min"], reverse=True)
                lift = training[0]
                lift_min[i] = int(round(lift["min"]))
                lift_cal[i] = lift["cal"]
                # Any remaining training sessions = evening hoops.
                bball.extend(training[1:])
            else:
                # Forgot to log the lift on Zepp — fall back to Notion estimate.
                lift_min[i] = int(n_min[i] or 0)
                lift_cal[i] = 0
        else:
            # No lift that day: every "training" session is basketball.
            bball.extend(training)

        bball_min[i] = round(sum(s["min"] for s in bball), 1)
        bball_cal[i] = int(sum(s["cal"] for s in bball))
        walk_min[i] = round(sum(s["min"] for s in walk), 1)
        walk_cal[i] = int(sum(s["cal"] for s in walk))

    return {
        "week_label": "Training — Last 7 Days",
        "labels": labels,
        "date_labels": date_labels,
        "lift_min": lift_min,
        "lift_cal": lift_cal,
        "lift_type": lift_type,
        "bball_min": bball_min,
        "bball_cal": bball_cal,
        "walk_min": walk_min,
        "walk_cal": walk_cal,
        "top_lifts": (notion or {}).get("top_lifts", []),
    }


# ---- Energy curve (derived) ----------------------------------------------

def derive_energy_curve(sleep: dict | None, activity: dict | None) -> dict:
    curve = [None] * 24

    def clip_hour(t_str: str) -> int | None:
        if not t_str or t_str == "—":
            return None
        try:
            return dt.datetime.strptime(t_str, "%I:%M %p").hour
        except Exception:
            return None

    wake_hr = clip_hour((sleep or {}).get("wake_label", "")) or 7
    bed_hr = clip_hour((sleep or {}).get("bedtime_label", "")) or 0

    if bed_hr >= 20:
        for h in range(bed_hr, 24):
            curve[h] = 5
        for h in range(0, wake_hr):
            curve[h] = 5
    else:
        for h in range(0, wake_hr):
            curve[h] = 5

    if wake_hr < 24:
        curve[wake_hr] = 28
        if wake_hr + 1 < 24 and curve[wake_hr + 1] is None:
            curve[wake_hr + 1] = 18

    for h in range(max(wake_hr + 2, 8), 11):
        if 0 <= h < 24:
            curve[h] = 14 + (h - wake_hr - 2) * 6

    peak_c = [65, 72, 78, 72]
    for i, h in enumerate(range(13, 17)):
        curve[h] = peak_c[i]
    curve[17] = 70
    peak_p = [78, 82, 78]
    for i, h in enumerate(range(18, 21)):
        curve[h] = peak_p[i]
    for i, h in enumerate(range(21, 24)):
        curve[h] = max(20, 60 - i * 14)

    last = 30
    for i in range(24):
        if curve[i] is None:
            curve[i] = last
        else:
            last = curve[i]

    wkout = (activity or {}).get("today_workout") or {}
    wkout_summary = (
        f"{wkout.get('type', 'No workout')} {wkout.get('duration_min', 0)} min"
        if wkout else "No workout"
    )
    footer = (
        f"Wake {(sleep or {}).get('wake_label', '—')}"
        f" · {wkout_summary} · Natural curve"
    )
    return {
        "date_label": now_pst().strftime("%b %-d"),
        "curve_24h": curve,
        "footer": footer,
    }


# ---- Main orchestrator ---------------------------------------------------

def _resolve_sections(spec: str) -> list[str]:
    if spec in PRESETS:
        return PRESETS[spec]
    return [s.strip() for s in spec.split(",") if s.strip()]


def main(out_path: Path, sections_spec: str) -> int:
    requested = _resolve_sections(sections_spec)
    previous = load_previous(out_path) or {}

    # Expand derived sections to the raw inputs they need.
    needed = set(requested)
    for sec in requested:
        needed.update(DERIVED_INPUTS.get(sec, []))

    # Decide which sources to contact.
    sources_to_run = [
        src for src, secs in SOURCE_SECTIONS.items()
        if any(s in needed for s in secs)
    ]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if "zepp" in sources_to_run:
            futures["zepp"] = pool.submit(fetch_zepp.fetch)
        if "cronometer" in sources_to_run:
            futures["cronometer"] = pool.submit(fetch_cronometer.fetch)
        if "notion" in sources_to_run:
            futures["notion"] = pool.submit(fetch_notion.fetch)
        results = {name: fut.result() for name, fut in futures.items()}

    # Start from previous so unrefreshed sections are preserved verbatim.
    payload: dict = {k: v for k, v in previous.items() if k != "_meta"}
    raw: dict = {}  # holds intermediate (_workouts_week, _strength_notion)
    meta_sources: dict = previous.get("_meta", {}).get("sources", {}).copy()

    allowed = set(needed)  # only write sections we were asked to refresh
    for src, result in results.items():
        meta_sources[src] = {
            "ok": result["ok"],
            "last_ok_iso": result["fetched_at_iso"] if result["ok"]
            else meta_sources.get(src, {}).get("last_ok_iso"),
            "error": result["error"],
        }
        if result["ok"] and result["data"]:
            for key, val in result["data"].items():
                # Notion calls its lift layer "strength_week"; stash as raw.
                if src == "notion" and key == "strength_week":
                    if "_strength_notion" in needed:
                        raw["_strength_notion"] = val
                elif key == "workouts_week":
                    if "_workouts_week" in needed:
                        raw["_workouts_week"] = val
                elif key in allowed:
                    payload[key] = val

    # Merge strength only when it was requested and an input was refreshed;
    # otherwise the previous strength_week is preserved untouched.
    if "strength_week" in requested and ("_strength_notion" in raw or "_workouts_week" in raw):
        merged = _merge_strength(
            raw.get("_strength_notion"),
            raw.get("_workouts_week"),
        )
        if merged:
            payload["strength_week"] = merged

    # Energy is derived from sleep + activity (only when those refreshed).
    if "energy" in needed and ("sleep" in payload or "activity" in payload):
        payload["energy"] = derive_energy_curve(
            payload.get("sleep"), payload.get("activity")
        )

    now = now_pst()
    payload["_meta"] = {
        "last_updated_iso": now.isoformat(timespec="seconds"),
        "last_updated_label": now.strftime("%b %-d, %-I:%M %p PST"),
        "refreshed_sections": requested,
        "sources": meta_sources,
        "schema_version": 2,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    print(f"[build_data] wrote {out_path} (sections={requested})")
    for src, meta in meta_sources.items():
        flag = "OK " if meta.get("ok") else "ERR"
        err = f" :: {meta.get('error')}" if meta.get("error") else ""
        print(f"  {flag} {src}{err}")

    if results and not any(m["ok"] for m in (results[s] for s in results)):
        return 2
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="health/widgets/data/data.json")
    ap.add_argument("--sections", default="full",
                    help="Preset (full/fast/nutrition) or comma list of sections")
    args = ap.parse_args()
    sys.exit(main(Path(args.out), args.sections))
