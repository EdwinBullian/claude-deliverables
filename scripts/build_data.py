"""Aggregate Zepp + Cronometer + Notion into a single ``data.json`` payload.

Concurrency: fetchers are run in parallel threads so a slow source (Zepp
sometimes takes 10–15s to walk the week) doesn't block the others.

Resilience: any source that fails is reported in ``_meta.sources`` and we
fall back to the previous ``data.json`` values for that source's sections,
so widgets keep showing the last good number rather than blank.

Output path: ``health/widgets/data/data.json`` (relative to repo root).
This way GitHub Pages can serve it from the same origin as the widgets.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fetch_cronometer
import fetch_notion
import fetch_zepp
from common import load_previous, now_pst

# Sections each source owns. The aggregator uses these to decide which
# previous values to restore when a source fails.
SECTIONS = {
    "zepp": ["sleep", "activity"],
    "cronometer": ["macros_week", "micros_week"],
    "notion": ["strength_week", "supplements"],
}


# ---- Energy curve (derived) ----------------------------------------------

def derive_energy_curve(sleep: dict | None, activity: dict | None) -> dict:
    """Synthesize a 24h energy curve from sleep + workout. This isn't from
    any wearable directly; it's a heuristic so the widget always renders.

    Anchor points:
      - Asleep block (bed → wake) clamped to 0–10
      - First 1.5h after wake: ramp 10 → 30
      - Cognitive peak 1–4 PM: 65–80
      - Physical peak 6–8 PM: 75–82
      - Wind down 9 PM+: declining ramp 60 → 25
    """
    curve = [None] * 24

    def clip_hour(t_str: str) -> int | None:
        """Parse '10:06 AM' → 10."""
        if not t_str or t_str == "—":
            return None
        try:
            return dt.datetime.strptime(t_str, "%I:%M %p").hour
        except Exception:
            return None

    wake_hr = clip_hour((sleep or {}).get("wake_label", "")) or 7
    bed_hr = clip_hour((sleep or {}).get("bedtime_label", "")) or 0

    # Sleep window
    if bed_hr >= 20:
        for h in range(bed_hr, 24):
            curve[h] = 5
        for h in range(0, wake_hr):
            curve[h] = 5
    else:
        for h in range(0, wake_hr):
            curve[h] = 5

    # Wake ramp
    if wake_hr < 24:
        curve[wake_hr] = 28
        if wake_hr + 1 < 24:
            curve[wake_hr + 1] = 18 if curve[wake_hr + 1] is None else curve[wake_hr + 1]

    # Mid-morning
    for h in range(max(wake_hr + 2, 8), 11):
        if 0 <= h < 24:
            curve[h] = 14 + (h - wake_hr - 2) * 6

    # Cognitive peak 13–16
    peak_c = [65, 72, 78, 72]
    for i, h in enumerate(range(13, 17)):
        curve[h] = peak_c[i]

    # Late afternoon dip
    curve[17] = 70

    # Physical peak 18–20
    peak_p = [78, 82, 78]
    for i, h in enumerate(range(18, 21)):
        curve[h] = peak_p[i]

    # Wind down 21+
    for i, h in enumerate(range(21, 24)):
        curve[h] = max(20, 60 - i * 14)

    # Fill any remaining None
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
        f" · {wkout_summary}"
        f" · Natural curve"
    )

    return {
        "date_label": now_pst().strftime("%b %-d"),
        "curve_24h": curve,
        "footer": footer,
    }


# ---- Main orchestrator ---------------------------------------------------

def main(out_path: Path) -> int:
    previous = load_previous(out_path)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "zepp": pool.submit(fetch_zepp.fetch),
            "cronometer": pool.submit(fetch_cronometer.fetch),
            "notion": pool.submit(fetch_notion.fetch),
        }
        results = {name: fut.result() for name, fut in futures.items()}

    payload: dict = {}
    meta_sources: dict = {}

    for src, result in results.items():
        meta_sources[src] = {
            "ok": result["ok"],
            "last_ok_iso": result["fetched_at_iso"] if result["ok"] else None,
            "error": result["error"],
        }
        if result["ok"] and result["data"]:
            payload.update(result["data"])

    # Restore previous sections for failed sources
    if previous:
        for src, sections in SECTIONS.items():
            if not results[src]["ok"]:
                meta_sources[src]["last_ok_iso"] = (
                    previous.get("_meta", {}).get("sources", {}).get(src, {}).get("last_ok_iso")
                )
                for sec in sections:
                    if sec in previous:
                        payload[sec] = previous[sec]

    # Derived: energy curve from sleep + activity
    payload["energy"] = derive_energy_curve(payload.get("sleep"), payload.get("activity"))

    # If no supplements pulled and we had previous ones, keep them
    if "supplements" not in payload and previous and "supplements" in previous:
        payload["supplements"] = previous["supplements"]

    now = now_pst()
    payload["_meta"] = {
        "last_updated_iso": now.isoformat(timespec="seconds"),
        "last_updated_label": now.strftime("%b %-d, %-I:%M %p PST"),
        "sources": meta_sources,
        "schema_version": 1,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    # Print summary for the GH Action log
    print(f"[build_data] wrote {out_path}")
    print(f"[build_data] sources:")
    for src, meta in meta_sources.items():
        flag = "OK " if meta["ok"] else "ERR"
        err = f" :: {meta['error']}" if meta["error"] else ""
        print(f"  {flag} {src}{err}")

    # Exit non-zero only if every source failed (so the workflow flags it
    # but a single failing source still publishes the partial update).
    return 0 if any(m["ok"] for m in meta_sources.values()) else 2


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("health/widgets/data/data.json")
    sys.exit(main(out))
