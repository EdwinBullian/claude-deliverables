"""Pull nutrition from Cronometer via Eddie's vendored Cronometer MCP.

Same strategy as fetch_zepp.py: vendor the MCP module, patch its config loader
to read env vars (GitHub Secrets), then call its existing _aggregate_day
function — which already handles login, GWT-RPC bootstrap, food caching, and
nutrient aggregation. Single source of truth for the auth + parsing logic.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor"))

import cronometer_mcp as cm  # noqa: E402

from common import (  # noqa: E402
    DAYS_SHORT,
    env,
    fail,
    ok,
    short_day_labels,
    today_pst,
    trailing_7_days,
)

TARGET_KCAL = int(env("CRONOMETER_TARGET_KCAL", default="2500") or 2500)
TARGET_PROTEIN_G = int(env("CRONOMETER_TARGET_PROTEIN_G", default="200") or 200)

RDI = {
    "Fiber (g)":        38,
    "Calcium (mg)":     1000,
    "Magnesium (mg)":   400,
    "Potassium (mg)":   4700,
    "Sodium (mg)":      2300,
    "Iron (mg)":        8,
    "Vitamin A (RAE) (mcg)": 900,
    "Vitamin C (mg)":   90,
}


def _env_config() -> dict:
    return {
        "cronometer": {
            "email":    env("CRONOMETER_EMAIL")    or "",
            "password": env("CRONOMETER_PASSWORD") or "",
        }
    }


cm._load_config = _env_config


def _has_data(day: dict) -> bool:
    if not day or not day.get("servings"):
        return False
    cals = (day.get("macros") or {}).get("calories", 0)
    return cals > 50


def _macro_score(avg_cal: float, avg_prot: float) -> int:
    cal_score = 50 if abs(avg_cal - TARGET_KCAL) / max(TARGET_KCAL, 1) <= 0.10 else int(
        max(0, 50 - abs(avg_cal - TARGET_KCAL) / max(TARGET_KCAL, 1) * 100)
    )
    prot_score = 50 if abs(avg_prot - TARGET_PROTEIN_G) / max(TARGET_PROTEIN_G, 1) <= 0.10 else int(
        max(0, 50 - abs(avg_prot - TARGET_PROTEIN_G) / max(TARGET_PROTEIN_G, 1) * 100)
    )
    return max(0, min(100, cal_score + prot_score))


def _fmt_amount(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    if unit == "mg" and v >= 1000:
        return f"{v/1000:.1f}g"
    if unit in ("g", "mcg"):
        return f"{int(round(v))}{unit}"
    return f"{int(round(v))}{unit}"


def _sub_label(value: float, rdi: float, unit: str) -> str:
    return f"{_fmt_amount(value, unit)} / {_fmt_amount(rdi, unit)}"


def fetch() -> dict:
    try:
        cm._get_state()
    except Exception as e:
        return fail(f"auth failed: {e}")

    try:
        today = today_pst()
        # Trailing 7-day window ending today, matching the activity + strength
        # widgets. No future days to filter out.
        week = trailing_7_days(today)
        days: list[dict] = []
        for d in week:
            try:
                days.append(cm._aggregate_day(d.isoformat()))
            except Exception:
                days.append({})

        protein_g = [int(round((d.get("macros") or {}).get("protein_g", 0))) if _has_data(d) else 0 for d in days]
        carbs_g   = [int(round((d.get("macros") or {}).get("carbs_g", 0)))   if _has_data(d) else 0 for d in days]
        fat_g     = [int(round((d.get("macros") or {}).get("fat_g", 0)))     if _has_data(d) else 0 for d in days]
        cals_arr  = [(d.get("macros") or {}).get("calories", 0)              if _has_data(d) else 0 for d in days]
        has_data  = [_has_data(d) for d in days]

        n = max(sum(has_data), 1)
        avg_protein = sum(protein_g) // n
        avg_carbs = sum(carbs_g) // n
        avg_fat = sum(fat_g) // n
        avg_cals = sum(cals_arr) / n

        score = _macro_score(avg_cals, avg_protein)

        logged_days = [d for d, h in zip(days, has_data) if h]

        def avg(name: str) -> float:
            if not logged_days:
                return 0.0
            return sum(d["all_nutrients"].get(name, 0.0) for d in logged_days) / len(logged_days)

        nutrients = [
            {"pct": int(avg("Fiber (g)")        / RDI["Fiber (g)"]        * 100),
             "label": "Fiber",     "sub": _sub_label(avg("Fiber (g)"),        RDI["Fiber (g)"],        "g"),
             "upper_limit": False},
            {"pct": int(avg("Calcium (mg)")     / RDI["Calcium (mg)"]     * 100),
             "label": "Calcium",   "sub": _sub_label(avg("Calcium (mg)"),     RDI["Calcium (mg)"],     "mg"),
             "upper_limit": False},
            {"pct": int(avg("Magnesium (mg)")   / RDI["Magnesium (mg)"]   * 100),
             "label": "Magnesium", "sub": _sub_label(avg("Magnesium (mg)"),   RDI["Magnesium (mg)"],   "mg"),
             "upper_limit": False},
            {"pct": int(avg("Potassium (mg)")   / RDI["Potassium (mg)"]   * 100),
             "label": "Potassium", "sub": _sub_label(avg("Potassium (mg)"),   RDI["Potassium (mg)"],   "mg"),
             "upper_limit": False},
            {"pct": int(avg("Sodium (mg)")      / RDI["Sodium (mg)"]      * 100),
             "label": "Sodium",    "sub": _sub_label(avg("Sodium (mg)"),      RDI["Sodium (mg)"],      "mg"),
             "upper_limit": True},
            {"pct": int(avg("Iron (mg)")        / RDI["Iron (mg)"]        * 100),
             "label": "Iron",      "sub": _sub_label(avg("Iron (mg)"),        RDI["Iron (mg)"],        "mg"),
             "upper_limit": False},
            {"pct": int(avg("Vitamin A (RAE) (mcg)") / RDI["Vitamin A (RAE) (mcg)"] * 100),
             "label": "Vitamin A", "sub": _sub_label(avg("Vitamin A (RAE) (mcg)"), RDI["Vitamin A (RAE) (mcg)"], "mcg"),
             "upper_limit": False},
            {"pct": int(avg("Vitamin C (mg)")   / RDI["Vitamin C (mg)"]   * 100),
             "label": "Vitamin C", "sub": _sub_label(avg("Vitamin C (mg)"),   RDI["Vitamin C (mg)"],   "mg"),
             "upper_limit": False},
        ]

        date_labels = [d.strftime("%b %-d") for d in week]
        n_logged = sum(has_data)
        day_labels = short_day_labels(week)

        macros_week = {
            "week_label": f"Macros — {date_labels[0]}–{date_labels[-1].split(' ')[-1]}",
            "macro_score_pct": score,
            "labels": day_labels,
            "dates": date_labels,
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "has_data": has_data,
            "target_kcal": TARGET_KCAL,
            "target_protein_g": TARGET_PROTEIN_G,
            "footer": f"Target: {TARGET_KCAL:,} kcal · {TARGET_PROTEIN_G}g protein · {n_logged}/7 days logged",
        }
        micros_week = {
            "week_label": "Nutrition — 7-Day Avg",
            "macro_score_pct": score,
            "days_logged": n_logged,
            "days_total": 7,
            "avg_protein_g": avg_protein,
            "avg_carbs_g": avg_carbs,
            "avg_fat_g": avg_fat,
            "target_kcal": TARGET_KCAL,
            "target_protein_g": TARGET_PROTEIN_G,
            "nutrients": nutrients,
            "footer": f"{n_logged} of 7 days logged · RDI for adult males · ↓ = upper limit",
        }

        return ok({"macros_week": macros_week, "micros_week": micros_week})
    except Exception as e:
        return fail(f"data pull failed: {e}")


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), indent=2, default=str))
