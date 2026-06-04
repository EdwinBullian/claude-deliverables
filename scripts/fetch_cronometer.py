"""Pull nutrition diary + macro/micro totals from Cronometer.

Cronometer has no public API. We log in to ``cronometer.com/login`` with the
same form the web app uses, capture the session cookie, then call the
internal ``/cronometer/app`` GWT-RPC endpoint that the SPA hits.

This style of auth has been stable for years; the lightweight client below
only depends on ``requests`` (no Selenium). If Cronometer changes the form
field names the fetcher will return a structured failure rather than crash
the workflow.
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
    today_pst,
    week_window_sun_to_sat,
)

HOST = "https://cronometer.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Daily targets — kept in env so they survive ration changes
TARGET_KCAL = int(env("CRONOMETER_TARGET_KCAL", default="2500") or 2500)
TARGET_PROTEIN_G = int(env("CRONOMETER_TARGET_PROTEIN_G", default="200") or 200)


def _login(session: requests.Session) -> str:
    """Log in and return the user id ('uid') the API expects."""
    email = env("CRONOMETER_EMAIL", required=True)
    password = env("CRONOMETER_PASSWORD", required=True)

    # 1. GET /login to harvest the anticsrf token from the page
    r = session.get(f"{HOST}/login", headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    csrf_match = re.search(r'name=["\']anticsrf["\']\s+value=["\']([^"\']+)["\']', r.text)
    csrf = csrf_match.group(1) if csrf_match else ""

    # 2. POST /login
    r = session.post(
        f"{HOST}/login",
        headers={"User-Agent": UA, "Referer": f"{HOST}/login"},
        data={
            "anticsrf": csrf,
            "username": email,
            "password": password,
        },
        allow_redirects=True,
        timeout=20,
    )
    if "logout" not in r.text.lower() and "dashboard" not in r.url:
        raise RuntimeError("login failed — credentials rejected or form changed")

    # 3. Land on /dashboard.html; the URL or a meta tag carries the uid
    uid_match = re.search(r'"user_id"\s*:\s*"?(\d+)"?', r.text) or re.search(r"uid=(\d+)", r.url)
    if not uid_match:
        raise RuntimeError("could not locate user id after login")
    return uid_match.group(1)


def _get_day_nutrients(session: requests.Session, uid: str, day: dt.date) -> dict | None:
    """Return totals for one day or None if not logged."""
    r = session.get(
        f"{HOST}/api/diary/nutrition_summary",
        params={"uid": uid, "date": day.isoformat()},
        headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _has_data(day_payload: dict | None) -> bool:
    if not day_payload:
        return False
    cals = day_payload.get("energy_kcal") or day_payload.get("calories") or 0
    return cals > 50  # ignore trivial trace entries


def _get_day_foods(session: requests.Session, uid: str, day: dt.date) -> list[dict]:
    """Return the per-food list for one day for the widget's tap-to-expand panel.

    Best-effort: if the diary endpoint shape changes we return [] rather than
    failing the whole nutrition pull (the widget then shows totals only).
    Each item: {name, amount, kcal, p, c, f}.
    """
    try:
        r = session.get(
            f"{HOST}/api/diary",
            params={"uid": uid, "date": day.isoformat()},
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        servings = r.json().get("servings", []) if isinstance(r.json(), dict) else []
    except Exception:
        return []

    out: list[dict] = []
    for s in servings:
        kcal = int(round(s.get("calories") or 0))
        if kcal < 5:  # skip water / zero-cal trace entries
            continue
        grams = s.get("grams")
        out.append({
            "name": s.get("food_name") or "Food",
            "amount": (f"{int(round(grams))} g" if grams else ""),
            "kcal": kcal,
            "p": int(round(s.get("protein_g") or 0)),
            "c": int(round(s.get("carbs_g") or 0)),
            "f": int(round(s.get("fat_g") or 0)),
        })
    return out


def _macro_score(day_payload: dict | None) -> int:
    """Quick score: 50 pts if calories within ±10% of target,
    50 pts if protein within ±10% of target.
    """
    if not _has_data(day_payload):
        return 0
    cals = day_payload.get("energy_kcal") or day_payload.get("calories") or 0
    prot = day_payload.get("protein_g") or day_payload.get("protein") or 0
    cal_score = 50 if abs(cals - TARGET_KCAL) / TARGET_KCAL <= 0.10 else int(
        max(0, 50 - abs(cals - TARGET_KCAL) / TARGET_KCAL * 100)
    )
    prot_score = 50 if abs(prot - TARGET_PROTEIN_G) / TARGET_PROTEIN_G <= 0.10 else int(
        max(0, 50 - abs(prot - TARGET_PROTEIN_G) / TARGET_PROTEIN_G * 100)
    )
    return max(0, min(100, cal_score + prot_score))


def _pct(part: float | None, whole: float | None) -> int:
    if not part or not whole:
        return 0
    return int(round(part / whole * 100))


def _sub(part: float | None, whole: float | None, unit: str) -> str:
    p = "—" if part is None else (f"{part/1000:.1f}g" if unit == "mg" and part >= 1000 else f"{int(round(part))}{unit}")
    w = "—" if whole is None else (f"{whole/1000:.1f}g" if unit == "mg" and whole >= 1000 else f"{int(round(whole))}{unit}")
    return f"{p} / {w}"


# Adult-male RDIs the widget assumes
RDI = {
    "fiber_g": 38,
    "calcium_mg": 1000,
    "magnesium_mg": 400,
    "potassium_mg": 4700,
    "sodium_mg": 2300,          # upper limit
    "iron_mg": 8,
    "vitamin_a_mcg": 900,
    "vitamin_c_mg": 90,
}


def fetch() -> dict:
    session = requests.Session()
    try:
        uid = _login(session)
    except Exception as e:
        return fail(f"auth failed: {e}")

    try:
        today = today_pst()
        week = week_window_sun_to_sat(today)
        days = [_get_day_nutrients(session, uid, d) for d in week]

        protein_g = [int(round((d.get("protein_g") if d else None) or 0)) for d in days]
        carbs_g = [int(round((d.get("carbs_g") if d else None) or 0)) for d in days]
        fat_g = [int(round((d.get("fat_g") if d else None) or 0)) for d in days]
        has_data = [_has_data(d) for d in days]

        # Strip future days
        for i, d in enumerate(week):
            if d > today:
                has_data[i] = False
                protein_g[i] = 0
                carbs_g[i] = 0
                fat_g[i] = 0

        score = _macro_score({
            "energy_kcal": sum((d.get("energy_kcal") or 0) for d in days if d) / max(sum(has_data), 1),
            "protein_g": sum(protein_g) / max(sum(has_data), 1),
        })

        # Week averages (only over days with data)
        n = max(sum(has_data), 1)
        avg_protein = sum(protein_g) // n
        avg_carbs = sum(carbs_g) // n
        avg_fat = sum(fat_g) // n

        # Micro average
        def avg(key: str) -> float:
            vals = [(d.get(key) or 0) for d, ok_ in zip(days, has_data) if ok_]
            return sum(vals) / max(len(vals), 1)

        nutrients = [
            {"pct": _pct(avg("fiber_g"), RDI["fiber_g"]),
             "label": "Fiber", "sub": _sub(avg("fiber_g"), RDI["fiber_g"], "g"),
             "upper_limit": False},
            {"pct": _pct(avg("calcium_mg"), RDI["calcium_mg"]),
             "label": "Calcium", "sub": _sub(avg("calcium_mg"), RDI["calcium_mg"], "mg"),
             "upper_limit": False},
            {"pct": _pct(avg("magnesium_mg"), RDI["magnesium_mg"]),
             "label": "Magnesium", "sub": _sub(avg("magnesium_mg"), RDI["magnesium_mg"], "mg"),
             "upper_limit": False},
            {"pct": _pct(avg("potassium_mg"), RDI["potassium_mg"]),
             "label": "Potassium", "sub": _sub(avg("potassium_mg"), RDI["potassium_mg"], "mg"),
             "upper_limit": False},
            {"pct": _pct(avg("sodium_mg"), RDI["sodium_mg"]),
             "label": "Sodium", "sub": _sub(avg("sodium_mg"), RDI["sodium_mg"], "mg"),
             "upper_limit": True},
            {"pct": _pct(avg("iron_mg"), RDI["iron_mg"]),
             "label": "Iron", "sub": _sub(avg("iron_mg"), RDI["iron_mg"], "mg"),
             "upper_limit": False},
            {"pct": _pct(avg("vitamin_a_mcg"), RDI["vitamin_a_mcg"]),
             "label": "Vitamin A", "sub": _sub(avg("vitamin_a_mcg"), RDI["vitamin_a_mcg"], " mcg"),
             "upper_limit": False},
            {"pct": _pct(avg("vitamin_c_mg"), RDI["vitamin_c_mg"]),
             "label": "Vitamin C", "sub": _sub(avg("vitamin_c_mg"), RDI["vitamin_c_mg"], "mg"),
             "upper_limit": False},
        ]

        labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        date_labels = [d.strftime("%b %-d") for d in week]
        week_label = f"W{week[0].isocalendar().week} Macros — {date_labels[0]}–{date_labels[-1].split(' ')[-1]}"
        n_logged = sum(has_data)

        # Per-day food list for the widget's tap-to-expand panel (only for
        # days with real data; future/empty days stay []).
        day_foods = [
            _get_day_foods(session, uid, d) if (has_data[i] and d <= today) else []
            for i, d in enumerate(week)
        ]

        macros_week = {
            "week_label": week_label,
            "macro_score_pct": score,
            "labels": labels,
            "dates": date_labels,
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "has_data": has_data,
            "day_foods": day_foods,
            "target_kcal": TARGET_KCAL,
            "target_protein_g": TARGET_PROTEIN_G,
            "footer": f"Target: {TARGET_KCAL:,} kcal · {TARGET_PROTEIN_G}g protein · {n_logged}/7 days logged",
        }
        micros_week = {
            "week_label": f"Nutrition — W{week[0].isocalendar().week} Avg",
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
