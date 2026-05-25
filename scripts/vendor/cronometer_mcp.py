#!/usr/bin/env python3
"""
Cronometer Nutrition MCP Server.

Reads Eddie's Cronometer diary via the v2 sessionKey (which doubles as the
GWT-RPC session token) and the public GWT-RPC service. We bypass the export
CSV path entirely — `generateExportToken` was removed from the service —
and instead use the same RPC calls Cronometer's own web app makes:

  * `getDayInfo(String token, Day day, int userId)`  → list of servings
    + exercises for a date.
  * `getFood(String token, int foodId)`              → per-food nutrition
    (per 100g) and English name, used for diary decoration and aggregation.

Wire format reverse-engineered from live browser captures, April 2026.
See `project_cronometer_mcp.md` in memory for the full breadcrumb trail.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
from mcp.server.fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

mcp = FastMCP("Cronometer Nutrition")

# ── GWT constants ─────────────────────────────────────────────────────────────
GWT_MODULE_BASE = "https://cronometer.com/cronometer/"
GWT_CT          = "text/x-gwt-rpc; charset=utf-8"
SVC             = "com.cronometer.shared.rpc.CronometerService"
POLICY          = "41DAAB5AB16D2C4398AEA0ABA379F290"  # the live policy
STR_TYPE        = "java.lang.String/2004016611"
INT_TYPE        = "java.lang.Integer/3438268394"
DAY_TYPE        = "com.cronometer.shared.entries.models.Day/782579793"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── Cached client state ───────────────────────────────────────────────────────
# We bootstrap a single requests.Session + sessionKey + perm hash, refresh
# at most once every 12 hours. All public tools go through `_get_state()`.
_state_lock = Lock()
_state: dict | None = None    # {"session", "sk", "uid", "perm", "expires_at"}

# In-memory food cache (food_id → {name, nutrients_per_100g})
_food_cache: dict[int, dict] = {}

# ── Cronometer reference data ────────────────────────────────────────────────
# Meal-slot encoding (fully verified Apr 27 2026 via 4 UI captures):
# Cronometer encodes meal_id as `(ordinal << 16) | 1` where:
#   0 = Uncategorized → 1
#   1 = Breakfast    → 65537
#   2 = Lunch        → 131073
#   3 = Dinner       → 196609
#   4 = Snacks       → 262145
# Eddie's older diary (2026-04-26) had values 65538/65539/65540 — those
# don't fit this scheme, but are tolerated by the server. Treating them
# as legacy Lunch/Dinner/Snacks for read display.
MEAL_NAMES = {
    1:      "Uncategorized",
    65537:  "Breakfast",
    131073: "Lunch",
    196609: "Dinner",
    262145: "Snacks",
    65538:  "Lunch (legacy)",
    65539:  "Dinner (legacy)",
    65540:  "Snacks (legacy)",
}

# Subset of Cronometer nutrient IDs we care about, mapped to display names.
# Sourced from the USDA Standard Reference + Cronometer's NutrientMap.
# Negative IDs are computed/derived nutrients (% energy from macros, etc.).
NUTRIENT_NAMES = {
    203: "Protein (g)",
    204: "Fat (g)",
    205: "Carbs (g)",
    207: "Ash (g)",
    208: "Energy (kcal)",
    209: "Starch (g)",
    210: "Sucrose (g)",
    211: "Glucose (g)",
    212: "Fructose (g)",
    213: "Lactose (g)",
    214: "Maltose (g)",
    221: "Alcohol (g)",
    255: "Water (g)",
    269: "Sugars (g)",
    287: "Galactose (g)",
    291: "Fiber (g)",
    295: "Soluble Fiber (g)",
    297: "Insoluble Fiber (g)",
    301: "Calcium (mg)",
    303: "Iron (mg)",
    304: "Magnesium (mg)",
    305: "Phosphorus (mg)",
    306: "Potassium (mg)",
    307: "Sodium (mg)",
    309: "Zinc (mg)",
    312: "Copper (mg)",
    315: "Manganese (mg)",
    317: "Selenium (mcg)",
    318: "Vitamin A (IU)",
    319: "Retinol (mcg)",
    320: "Vitamin A (RAE) (mcg)",
    321: "Carotene, alpha (mcg)",
    322: "Carotene, beta (mcg)",
    323: "Vitamin E (mg)",
    324: "Vitamin D (IU)",
    325: "Vitamin D2 (mcg)",
    326: "Vitamin D3 (mcg)",
    334: "Cryptoxanthin, beta (mcg)",
    337: "Lycopene (mcg)",
    338: "Lutein+Zeaxanthin (mcg)",
    341: "Tocopherol, beta (mg)",
    342: "Tocopherol, gamma (mg)",
    343: "Tocopherol, delta (mg)",
    401: "Vitamin C (mg)",
    404: "Thiamine (B1) (mg)",
    405: "Riboflavin (B2) (mg)",
    406: "Niacin (B3) (mg)",
    410: "Pantothenic Acid (B5) (mg)",
    415: "Vitamin B6 (mg)",
    417: "Folate (mcg)",
    418: "Vitamin B12 (mcg)",
    421: "Choline (mg)",
    430: "Vitamin K (mcg)",
    432: "Folate, food (mcg)",
    435: "Folate, DFE (mcg)",
    454: "Betaine (mg)",
    501: "Tryptophan (g)",
    502: "Threonine (g)",
    503: "Isoleucine (g)",
    504: "Leucine (g)",
    505: "Lysine (g)",
    506: "Methionine (g)",
    507: "Cystine (g)",
    508: "Phenylalanine (g)",
    509: "Tyrosine (g)",
    510: "Valine (g)",
    511: "Arginine (g)",
    512: "Histidine (g)",
    513: "Alanine (g)",
    514: "Aspartic acid (g)",
    515: "Glutamic acid (g)",
    516: "Glycine (g)",
    517: "Proline (g)",
    518: "Serine (g)",
    601: "Cholesterol (mg)",
    605: "Trans-Fats (g)",
    606: "Saturated (g)",
    645: "Monounsaturated (g)",
    646: "Polyunsaturated (g)",
    675: "Omega-3 (g)",
    851: "Omega-6 (g)",
    853: "Omega-3 (ALA) (g)",
    -221: "Net Carbs (g)",
    -1205: "Net Carbs (g)",
}

# Per-food %-of-energy fields (Cronometer ids -203/-204/-205) — these mean
# something on a single food but are nonsense when naively summed across the
# diary, so we drop them during aggregation and recompute from total kcal.
_PER_FOOD_PCT_IDS = {-203, -204, -205}

# Macros we expose as the headline numbers in summaries.
MACRO_KEYS = {
    "calories":  208,
    "protein_g": 203,
    "fat_g":     204,
    "carbs_g":   205,
    "fiber_g":   291,
    "sugar_g":   269,
    "sodium_mg": 307,
}


# ── Auth + GWT bootstrap ──────────────────────────────────────────────────────

def _get_state() -> dict:
    """Return cached client state {session, sk, uid, perm}, refreshing if stale."""
    global _state
    with _state_lock:
        now = time.time()
        if _state and _state["expires_at"] > now:
            return _state

        cfg = _load_config()["cronometer"]
        s = requests.Session()
        s.headers["User-Agent"] = UA

        # Touch the homepage first to receive the AWSALB load-balancer cookie
        # (some routes 502 without it).
        s.get("https://cronometer.com/", timeout=10)

        login = s.post(
            "https://cronometer.com/api/v2/login",
            json={"email": cfg["email"], "password": cfg["password"]},
            headers={"Content-Type": "application/json",
                      "Accept": "application/json"},
            timeout=15,
        )
        login.raise_for_status()
        data = login.json()
        if data.get("result") != "SUCCESS" or "sessionKey" not in data:
            raise RuntimeError(
                f"Cronometer v2 login failed: {data.get('error', data)}. "
                f"If rate-limited, wait a few minutes and retry."
            )
        sk  = data["sessionKey"]
        uid = data.get("id", 0)

        # Permutation hash powers the X-GWT-Permutation header. It rotates
        # with each Cronometer deploy, so we always re-fetch on bootstrap.
        nocache = s.get(f"{GWT_MODULE_BASE}cronometer.nocache.js", timeout=10)
        perms = re.findall(r'["\']([0-9A-F]{32})["\']', nocache.text)
        if not perms:
            raise RuntimeError("Could not extract GWT permutation hash from nocache.js")
        perm = perms[0]

        _state = {
            "session":    s,
            "sk":         sk,
            "uid":        uid,
            "perm":       perm,
            "expires_at": now + 12 * 3600,
        }
        return _state


def _gwt_post(body: str) -> requests.Response:
    """POST a GWT-RPC body to /cronometer/app and return the response."""
    st = _get_state()
    return st["session"].post(
        f"{GWT_MODULE_BASE}app",
        data=body,
        headers={
            "Content-Type":      GWT_CT,
            "X-GWT-Permutation": st["perm"],
            "X-GWT-Module-Base": GWT_MODULE_BASE,
            "Referer":           "https://cronometer.com/",
            "Origin":            "https://cronometer.com",
            "Accept":            "*/*",
        },
        timeout=20,
    )


def _check_ok(text: str, ctx: str) -> str:
    """Raise on //EX, return text on //OK."""
    if text.startswith("//EX"):
        raise RuntimeError(f"GWT call {ctx} returned exception: {text[:400]}")
    if not text.startswith("//OK"):
        raise RuntimeError(f"GWT call {ctx} unexpected response: {text[:200]}")
    return text


# ── Date helpers ──────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _date_range(start: str, end: str) -> list[str]:
    """Inclusive list of dates from start to end (both YYYY-MM-DD)."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end,   "%Y-%m-%d")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


# ── getDayInfo: diary structure for one date ─────────────────────────────────
# Wire format (reverse-engineered):
#   getDayInfo(String token, Day{day,month,year} day, int userId)
# String table count = 8: MODULE, POLICY, SVC, "getDayInfo", STR_TYPE,
#                         DAY_TYPE, "I", token
# Body: 1|2|3|4|3|5|6|7|8|6|<day>|<month>|<year>|<userId>|

def _build_day_info_body(token: str, day: int, month: int, year: int,
                          user_id: int) -> str:
    return (
        f"7|0|8|{GWT_MODULE_BASE}|{POLICY}|{SVC}|getDayInfo|"
        f"{STR_TYPE}|{DAY_TYPE}|I|{token}|"
        f"1|2|3|4|3|5|6|7|8|6|{day}|{month}|{year}|{user_id}|"
    )


# Each Serving record in the response carries 14+ fields, anchored on a
# 5-7 char base62 UUID (e.g. "EK8HTC", "ELNZiy"). The Serving class ref
# appears at the end (varies per response — 9, 4, etc. depending on the
# type-table layout), so we tolerate any positive int there. meal_id can
# be 1 (Uncategorized) or 65537+ (Breakfast/Lunch/Dinner/Snacks).
_SERVING_RE = re.compile(
    r'"([A-Za-z0-9_]{4,8})",(\d+),(\d+(?:\.\d+)?),(\d+),0,(\d+),'
    r'0,1,1,(\d+),(\d+),(\d+),\d+,\d+'
)


def _parse_day_info(text: str) -> list[dict]:
    """Extract serving entries from a getDayInfo //OK response."""
    out = []
    for m in _SERVING_RE.finditer(text):
        uuid, food_id, grams, _uid, meal_id, year, month, day = m.groups()
        out.append({
            "uuid":    uuid,
            "food_id": int(food_id),
            "grams":   float(grams),
            "meal_id": int(meal_id),
            "meal":    MEAL_NAMES.get(int(meal_id), f"Meal {meal_id}"),
            "date":    f"{int(year):04d}-{int(month):02d}-{int(day):02d}",
        })
    # Cronometer returns servings in reverse-meal order (snacks → breakfast).
    # Sort to natural meal order so the diary reads top-down chronologically.
    out.sort(key=lambda s: (s["meal_id"], s["uuid"]))
    return out


def _fetch_day_info(date: str) -> list[dict]:
    """Call getDayInfo for a date and return the parsed serving list."""
    st = _get_state()
    y, mo, d = (int(x) for x in date.split("-"))
    body = _build_day_info_body(st["sk"], d, mo, y, st["uid"])
    r = _gwt_post(body)
    r.raise_for_status()
    _check_ok(r.text, f"getDayInfo({date})")
    return _parse_day_info(r.text)


# ── getFood: per-food nutrition ──────────────────────────────────────────────
# Wire format:
#   getFood(String token, int foodId)
# String table count = 7: MODULE, POLICY, SVC, "getFood", STR_TYPE, "I", token
# Body: 1|2|3|4|2|5|6|7|<foodId>|

def _build_get_food_body(token: str, food_id: int) -> str:
    return (
        f"7|0|7|{GWT_MODULE_BASE}|{POLICY}|{SVC}|getFood|"
        f"{STR_TYPE}|I|{token}|"
        f"1|2|3|4|2|5|6|7|{food_id}|"
    )


# Each nutrient appears in the response as `<id>,<value>,<marker>,<id>,...`
# — the same nutrient ID appears twice with the value sandwiched between.
# We anchor on that double-occurrence to filter out random integers.
_NUTRIENT_RE = re.compile(r',(-?\d{2,5}),(-?\d+(?:\.\d+)?(?:e-?\d+)?),\d+,(-?\d{2,5}),')

# The English food name is the first non-flag string immediately after the US
# flag-image URL in the type table at the end of the response.
_NAME_RE = re.compile(
    r'"https://cdn1\.cronometer\.com/media/flags/us\.png","([^"]+)"'
)


def _parse_food_response(text: str) -> dict:
    """Extract food name + per-100g nutrient dict from a getFood //OK response."""
    nutrients: dict[int, float] = {}
    for m in _NUTRIENT_RE.finditer(text):
        a, val, b = m.groups()
        if a != b:
            continue   # only accept paired nutrient_id / nutrient_id sandwich
        try:
            v = float(val)
        except ValueError:
            continue
        nid = int(a)
        # If we somehow see the same nutrient twice, last wins; that matches
        # how Cronometer would have it in the most-recently-written record.
        nutrients[nid] = v

    name_m = _NAME_RE.search(text)
    name = name_m.group(1) if name_m else ""

    return {"name": name, "nutrients_per_100g": nutrients}


def _fetch_food(food_id: int) -> dict:
    """Get cached food details by id; fetch + cache on miss."""
    if food_id in _food_cache:
        return _food_cache[food_id]
    st = _get_state()
    body = _build_get_food_body(st["sk"], food_id)
    r = _gwt_post(body)
    r.raise_for_status()
    if r.text.startswith("//EX"):
        # Don't blow up the whole diary if one food lookup fails — return an
        # empty record so the rest of the day still works.
        rec = {"name": f"<food {food_id} unavailable>", "nutrients_per_100g": {}}
    else:
        rec = _parse_food_response(r.text)
    _food_cache[food_id] = rec
    return rec


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate_day(date: str) -> dict:
    """Pull diary + food nutrition for a date, return totals and per-serving rows."""
    servings = _fetch_day_info(date)

    # Resolve every food once. Many days will have ≤6 distinct foods so this
    # is cheap; the cache also amortizes across multi-day queries.
    foods: dict[int, dict] = {}
    for s in servings:
        if s["food_id"] not in foods:
            foods[s["food_id"]] = _fetch_food(s["food_id"])

    totals: dict[int, float] = {}
    rows = []
    for s in servings:
        food   = foods[s["food_id"]]
        scale  = s["grams"] / 100.0
        scaled = {nid: v * scale for nid, v in food["nutrients_per_100g"].items()}
        for nid, v in scaled.items():
            if nid in _PER_FOOD_PCT_IDS:
                continue   # don't sum per-food percent-of-energy fields
            totals[nid] = totals.get(nid, 0.0) + v
        rows.append({
            **s,
            "food_name": food["name"],
            "calories":  round(scaled.get(208, 0.0), 1),
            "protein_g": round(scaled.get(203, 0.0), 2),
            "fat_g":     round(scaled.get(204, 0.0), 2),
            "carbs_g":   round(scaled.get(205, 0.0), 2),
            "fiber_g":   round(scaled.get(291, 0.0), 2),
        })

    # Build a clean, named totals dict
    named_totals = {
        NUTRIENT_NAMES.get(nid, f"nutrient_{nid}"): round(v, 2)
        for nid, v in totals.items()
        if nid in NUTRIENT_NAMES
    }
    macro_totals = {k: round(totals.get(nid, 0.0), 1) for k, nid in MACRO_KEYS.items()}

    # Recompute %-of-energy macros from totals — meaningful at the day level.
    cal = totals.get(208, 0.0)
    if cal > 0:
        named_totals["Protein (% kcal)"] = round(totals.get(203, 0.0) * 4 / cal * 100, 1)
        named_totals["Fat (% kcal)"]     = round(totals.get(204, 0.0) * 9 / cal * 100, 1)
        named_totals["Carbs (% kcal)"]   = round(totals.get(205, 0.0) * 4 / cal * 100, 1)

    return {
        "date":            date,
        "servings":        rows,
        "macros":          macro_totals,
        "all_nutrients":   named_totals,
    }


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def get_food_diary(date: str = "") -> str:
    """
    Eddie's full food diary for a given date — every food item logged with
    serving size, meal slot, calories, and macros. Date is YYYY-MM-DD;
    defaults to today.
    """
    date = date or _today()
    try:
        day = _aggregate_day(date)
    except Exception as e:
        return f"Failed to load diary for {date}: {e}"
    if not day["servings"]:
        return f"No food entries logged for {date}."
    return json.dumps({
        "date":     date,
        "servings": day["servings"],
        "totals":   day["macros"],
    }, indent=2)


@mcp.tool()
def get_nutrition_summary(date: str = "") -> str:
    """
    Eddie's complete macro and micronutrient totals for a given date.
    Pulls every food in the diary and sums nutrients across all servings.
    Date is YYYY-MM-DD; defaults to today.
    """
    date = date or _today()
    try:
        day = _aggregate_day(date)
    except Exception as e:
        return f"Failed to load nutrition summary for {date}: {e}"
    if not day["servings"]:
        return f"No food entries logged for {date}."
    return json.dumps({
        "date":      date,
        "servings":  len(day["servings"]),
        "macros":    day["macros"],
        "nutrients": day["all_nutrients"],
    }, indent=2)


@mcp.tool()
def get_nutrition_range(start_date: str, end_date: str) -> str:
    """
    Eddie's daily macro totals across a date range. Useful for weekly / monthly
    macro averages. Both dates YYYY-MM-DD, inclusive.
    """
    dates = _date_range(start_date, end_date)
    daily = []
    for d in dates:
        try:
            day = _aggregate_day(d)
        except Exception as e:
            daily.append({"date": d, "error": str(e)})
            continue
        if day["servings"]:
            daily.append({"date": d, **day["macros"]})
    if not daily:
        return f"No nutrition data found between {start_date} and {end_date}."
    return json.dumps({
        "period": f"{start_date} to {end_date}",
        "days":   len(daily),
        "daily":  daily,
    }, indent=2)


@mcp.tool()
def get_weekly_nutrition() -> str:
    """
    Eddie's nutrition for the past 7 days, with per-day macros and 7-day
    averages of calories, protein, carbs, fat, and fiber.
    """
    end   = _today()
    start = _days_ago(6)
    dates = _date_range(start, end)

    daily = []
    sums  = {k: 0.0 for k in MACRO_KEYS}
    n     = 0
    for d in dates:
        try:
            day = _aggregate_day(d)
        except Exception as e:
            daily.append({"date": d, "error": str(e)})
            continue
        if not day["servings"]:
            continue
        daily.append({"date": d, **day["macros"]})
        for k, v in day["macros"].items():
            sums[k] += v
        n += 1
    if not n:
        return "No nutrition data found for the past 7 days."
    averages = {k: round(sums[k] / n, 1) for k in MACRO_KEYS}
    return json.dumps({
        "period":   f"{start} to {end}",
        "days":     n,
        "averages": averages,
        "daily":    daily,
    }, indent=2)


@mcp.tool()
def check_macro_goals(date: str = "", protein_goal: float = 180,
                      calorie_goal: float = 2800) -> str:
    """
    Compare Eddie's actual intake to his macro goals for a given date.
    Carbs/fat goals are derived from calorie_goal at 40% / 30%; fiber goal
    is fixed at 35g. Date YYYY-MM-DD; defaults to today.
    """
    date = date or _today()
    try:
        day = _aggregate_day(date)
    except Exception as e:
        return f"Failed to load nutrition for {date}: {e}"
    if not day["servings"]:
        return f"No food entries logged for {date}."

    m = day["macros"]
    actual_cal     = m["calories"]
    actual_protein = m["protein_g"]
    actual_carbs   = m["carbs_g"]
    actual_fat     = m["fat_g"]
    actual_fiber   = m["fiber_g"]

    carb_goal  = round((calorie_goal * 0.40) / 4, 0)
    fat_goal   = round((calorie_goal * 0.30) / 9, 0)
    fiber_goal = 35.0

    return json.dumps({
        "date": date,
        "calories":  {"actual": actual_cal,     "goal": calorie_goal,
                       "remaining": round(calorie_goal - actual_cal, 1)},
        "protein_g": {"actual": actual_protein, "goal": protein_goal,
                       "remaining": round(protein_goal - actual_protein, 1)},
        "carbs_g":   {"actual": actual_carbs,   "goal": carb_goal,
                       "remaining": round(carb_goal - actual_carbs, 1)},
        "fat_g":     {"actual": actual_fat,     "goal": fat_goal,
                       "remaining": round(fat_goal - actual_fat, 1)},
        "fiber_g":   {"actual": actual_fiber,   "goal": fiber_goal,
                       "remaining": round(fiber_goal - actual_fiber, 1)},
    }, indent=2)


@mcp.tool()
def refresh_session() -> str:
    """
    Force-clear cached session state. Call this if you suspect the v2
    sessionKey or GWT permutation has gone stale (e.g. after a Cronometer
    deploy or a rate-limit window has cooled).
    """
    global _state, _food_cache
    with _state_lock:
        _state = None
        _food_cache.clear()
    st = _get_state()
    return json.dumps({
        "status":          "refreshed",
        "session_key_8":   st["sk"][:8] + "...",
        "user_id":         st["uid"],
        "perm_hash":       st["perm"],
        "food_cache_size": len(_food_cache),
    }, indent=2)


# ── Debug tools (kept lean — full reverse-engineering log is in memory) ──────

@mcp.tool()
def debug_state() -> str:
    """
    Show current cached client state — sessionKey prefix, userId, permutation
    hash, and food-cache size. Useful for quickly checking that auth is alive
    without making a real diary call.
    """
    try:
        st = _get_state()
        return json.dumps({
            "session_key_8":   st["sk"][:8] + "...",
            "user_id":         st["uid"],
            "perm_hash":       st["perm"],
            "expires_in_sec":  int(st["expires_at"] - time.time()),
            "food_cache_size": len(_food_cache),
            "food_cache_ids":  list(_food_cache.keys())[:20],
        }, indent=2)
    except Exception as e:
        return f"State unavailable: {e}"


@mcp.tool()
def debug_day_info(date: str = "") -> str:
    """
    Run getDayInfo for a date and return the raw response plus parsed
    serving list. Useful to confirm the diary call is wired correctly
    without going through the food-resolution path.
    """
    date = date or _today()
    try:
        st = _get_state()
        y, mo, d = (int(x) for x in date.split("-"))
        body = _build_day_info_body(st["sk"], d, mo, y, st["uid"])
        r = _gwt_post(body)
        return json.dumps({
            "date":             date,
            "request_body":     body,
            "response_status":  r.status_code,
            "response_starts":  r.text[:6],
            "response_length":  len(r.text),
            "response_first_2k": r.text[:2000],
            "parsed_servings":  _parse_day_info(r.text)
                                if r.text.startswith("//OK") else [],
        }, indent=2)
    except Exception as e:
        return f"debug_day_info failed: {e}"


@mcp.tool()
def debug_food(food_id: int) -> str:
    """
    Run getFood for a food_id and return the parsed name + nutrients
    (and a slice of the raw response). Useful to spot foods whose values
    don't look like per-100g (e.g. label-style custom foods).
    """
    try:
        st = _get_state()
        body = _build_get_food_body(st["sk"], food_id)
        r = _gwt_post(body)
        parsed = _parse_food_response(r.text) if r.text.startswith("//OK") else {}
        return json.dumps({
            "food_id":          food_id,
            "response_status":  r.status_code,
            "response_starts":  r.text[:6],
            "response_length":  len(r.text),
            "response_first_2k": r.text[:2000],
            "parsed":           parsed,
        }, indent=2)
    except Exception as e:
        return f"debug_food failed: {e}"


# ── Write API: search, list-my-foods, remove, add ────────────────────────────
# These methods were captured from live browser traffic (Apr 27 2026). The
# read methods (getDayInfo, getFood) are the same shape as before; the new
# methods follow the same string-table-then-body GWT v7 conventions.
#
# Decoded confidently:
#   findFoods(token, query, limit, FoodSource[], …)
#   findMyFoods(token, userId)
#   getRawIngredientString(token, userId, recipeId)
#   canRecipeInclude(recipeId, candidateFoodId)
#   removeServing(token, servingUuid (long), userId)
#
# Decoded with one unverified field (meal slot encoding):
#   updateDiary(token, userId, List<AddEntryChange>)


def _build_find_foods_body(token: str, query: str, limit: int = 50) -> str:
    """Build a findFoods GWT body. Searches the global food DB for `query`.

    Wire format reverse-engineered: paramCount=8, types=String,String,int,
    FoodSource[],int,String,FoodSearchTabSelection,boolean. The query
    string lives in slot 11 of the table; we splice it in directly.
    """
    return (
        f"7|0|12|{GWT_MODULE_BASE}|{POLICY}|{SVC}|findFoods|"
        f"{STR_TYPE}|I|"
        f"[Lcom.cronometer.shared.foods.FoodSource;/3597302983|"
        f"com.cronometer.shared.foods.FoodSearchTabSelection/1776179901|Z|"
        f"{token}|{query}|"
        f"com.cronometer.shared.foods.FoodSource/4236433762|"
        f"1|2|3|4|8|5|5|6|7|6|5|8|9|10|11|{limit}|7|1|12|0|0|0|8|2|0|"
    )


def _build_find_my_foods_body(token: str, user_id: int) -> str:
    return (
        f"7|0|7|{GWT_MODULE_BASE}|{POLICY}|{SVC}|findMyFoods|"
        f"{STR_TYPE}|I|{token}|"
        f"1|2|3|4|2|5|6|7|{user_id}|"
    )


def _build_get_raw_ingredient_string_body(token: str, user_id: int,
                                           recipe_id: int) -> str:
    return (
        f"7|0|7|{GWT_MODULE_BASE}|{POLICY}|{SVC}|getRawIngredientString|"
        f"{STR_TYPE}|I|{token}|"
        f"1|2|3|4|3|5|6|6|7|{user_id}|{recipe_id}|"
    )


def _build_can_recipe_include_body(recipe_id: int,
                                    candidate_food_id: int) -> str:
    """Note: this method takes only two ints, no auth token — it's a static
    cycle-detection check the client uses before letting you nest a recipe."""
    return (
        f"7|0|5|{GWT_MODULE_BASE}|{POLICY}|{SVC}|canRecipeInclude|I|"
        f"1|2|3|4|2|5|5|{recipe_id}|{candidate_food_id}|"
    )


def _build_remove_serving_body(token: str, serving_uuid: str,
                                user_id: int) -> str:
    """removeServing(String token, long servingId, int userId).

    Cronometer's serving "UUID" is actually a base62-encoded long. We pass
    it through inline as a string and the server interprets it as long.
    """
    return (
        f"7|0|8|{GWT_MODULE_BASE}|{POLICY}|{SVC}|removeServing|"
        f"{STR_TYPE}|J|I|{token}|"
        f"1|2|3|4|3|5|6|7|8|{serving_uuid}|{user_id}|"
    )


# Wire encoding for updateDiary, fully decoded after diffing two captures
# (one for an Uncategorized food entry, one for a Breakfast recipe entry).
# The 12 fields after the Day struct are:
#   field 1-3: 1, 1, 0  (constant headers — server-managed flags)
#   field 4:   meal_id  (1=Uncategorized, 65537=Breakfast, 65538=Lunch,
#                        65539=Dinner, 65540=Snacks — same values
#                        getDayInfo returns)
#   field 5-6: 0, 0     (constant)
#   field 7:   amount   (grams for raw foods, serving count for recipes)
#   field 8:   food_id  (a built-in food id OR a custom recipe id)
#   field 9:   "A"      (char status flag, constant)
#   field 10:  measure_id (Cronometer measure id; defines unit of `amount`)
#   field 11-12: 0, 0   (constant)

# Reverse map for the meal field. Friendly names map to the bit-flag IDs
# that Cronometer's UI actually recognizes (verified Apr 27 2026). Pass
# any int directly to override.
_MEAL_ID = {
    1:                 1,
    "uncategorized":   1,    "Uncategorized": 1,
    65537:             65537,
    "breakfast":       65537, "Breakfast":    65537,
    131073:            131073,
    "lunch":           131073, "Lunch":       131073,
    196609:            196609,
    "dinner":          196609, "Dinner":      196609,
    262145:            262145,
    "snacks":          262145, "Snacks":      262145, "snack": 262145,
}


def _build_update_diary_add_body(
    token: str, user_id: int, food_id: int, amount: float,
    measure_id: int, meal: int | str,
    day: int, month: int, year: int,
) -> str:
    meal_id = _MEAL_ID.get(meal, 1)
    return (
        f"7|0|12|{GWT_MODULE_BASE}|{POLICY}|{SVC}|updateDiary|"
        f"{STR_TYPE}|I|java.util.List|{token}|"
        f"java.util.Collections$SingletonList/1586180994|"
        f"com.cronometer.shared.entries.changes.AddEntryChange/3949104564|"
        f"com.cronometer.shared.entries.models.Serving/2553599101|"
        f"com.cronometer.shared.entries.models.Day/782579793|"
        f"1|2|3|4|3|5|6|7|8|{user_id}|"
        f"9|10|1|1|11|12|{day}|{month}|{year}|"
        f"1|1|0|{meal_id}|0|0|{int(amount)}|{food_id}|A|{measure_id}|0|0|"
    )


# ── Write tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def search_foods(query: str, limit: int = 25) -> str:
    """
    Search Cronometer's global food database for `query` (e.g. "chicken
    burrito bowl", "Greek yogurt"). Returns matching foods with their
    food_id — feed an id into add_to_diary or get_food_nutrition.
    """
    try:
        st = _get_state()
        body = _build_find_foods_body(st["sk"], query, limit)
        r = _gwt_post(body)
        r.raise_for_status()
        if r.text.startswith("//EX"):
            return f"Search failed: {r.text[:300]}"
        # Foods come back with name strings near the end of the response;
        # we extract food_id + name pairs heuristically.
        # food_id appears as a bare int followed by a name in the type table.
        # For now return the raw response so Eddie can spot foods, until we
        # build a proper parser for the search response.
        names = re.findall(r'"([^"]{4,80})"', r.text)
        ids   = re.findall(r',(\d{4,9}),', r.text)
        return json.dumps({
            "query":      query,
            "limit":      limit,
            "name_hits":  names[:limit],
            "id_hits":    list(dict.fromkeys(ids))[:limit],
            "raw_first_2k": r.text[:2000],
        }, indent=2)
    except Exception as e:
        return f"search_foods failed: {e}"


# Each entry in a findMyFoods response: <class_ref>,0,0,-3,0,0,<seq>,0,0,0,<food_id>,0,0,2,0
# Class ref is one of -4 / -6 / -19 (different food-type categories).
# Names live at the end of the response in the type table, in the same
# order as the entries.
_MY_FOOD_ENTRY_RE = re.compile(
    r'-?\d+,0,0,-3,0,0,\d+,0,0,0,(\d+),0,0,2,0'
)


def _parse_my_foods(text: str) -> list[dict]:
    """Walk the findMyFoods response and pair each food_id with its name.

    Cronometer emits entries newest-first (descending seq) but names in
    the type table are written in order of first reference, which ends up
    oldest-first. So we reverse the names list before zipping them with
    food_ids. Verified via Breakfast Bowl: seq 39 / food_id 47697913
    correctly pairs with the "Breakfast Bowl" name slot.
    """
    food_ids = [int(m) for m in _MY_FOOD_ENTRY_RE.findall(text)]
    all_strings = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    names = [n for n in all_strings
              if "." not in n and "/" not in n
              and not n.startswith("com.cronometer")
              and not n.startswith("java.")
              and not n.startswith("https://")
              and len(n) >= 2]
    names_newest_first = list(reversed(names))
    pairs = []
    for i, food_id in enumerate(food_ids):
        name = names_newest_first[i] if i < len(names_newest_first) else "<unknown>"
        pairs.append({"food_id": food_id, "name": name})
    return pairs


@mcp.tool()
def list_my_foods() -> str:
    """
    List Eddie's custom foods + recipes with food_ids paired to names.
    Use this to look up a recipe's food_id before passing it to add_to_diary.
    """
    try:
        st = _get_state()
        body = _build_find_my_foods_body(st["sk"], st["uid"])
        r = _gwt_post(body)
        r.raise_for_status()
        if r.text.startswith("//EX"):
            return f"findMyFoods failed: {r.text[:300]}"
        pairs = _parse_my_foods(r.text)
        return json.dumps({
            "count":  len(pairs),
            "foods":  pairs,
        }, indent=2)
    except Exception as e:
        return f"list_my_foods failed: {e}"


@mcp.tool()
def find_my_food(query: str) -> str:
    """
    Look up Eddie's custom food/recipe by name (case-insensitive substring).
    Returns matching {food_id, name} pairs. Useful when you want to add a
    saved recipe to the diary by name.
    """
    try:
        st = _get_state()
        body = _build_find_my_foods_body(st["sk"], st["uid"])
        r = _gwt_post(body)
        r.raise_for_status()
        if r.text.startswith("//EX"):
            return f"findMyFoods failed: {r.text[:300]}"
        pairs = _parse_my_foods(r.text)
        q = query.lower()
        matches = [p for p in pairs if q in p["name"].lower()]
        return json.dumps({
            "query":   query,
            "matches": matches,
        }, indent=2)
    except Exception as e:
        return f"find_my_food failed: {e}"


@mcp.tool()
def get_recipe_ingredients(recipe_id: int) -> str:
    """
    Get the raw ingredient string for one of Eddie's custom recipes.
    Useful when you want to see what's already in a saved recipe before
    editing or duplicating it.
    """
    try:
        st = _get_state()
        body = _build_get_raw_ingredient_string_body(st["sk"], st["uid"], recipe_id)
        r = _gwt_post(body)
        r.raise_for_status()
        if r.text.startswith("//EX"):
            return f"getRawIngredientString failed: {r.text[:300]}"
        return json.dumps({
            "recipe_id":    recipe_id,
            "raw":          r.text,
        }, indent=2)
    except Exception as e:
        return f"get_recipe_ingredients failed: {e}"


@mcp.tool()
def remove_from_diary(serving_uuid: str) -> str:
    """
    Delete a food entry from Eddie's diary by its 6-character serving UUID
    (e.g. "EK8HTC" — the `uuid` field returned by get_food_diary).
    """
    try:
        st = _get_state()
        body = _build_remove_serving_body(st["sk"], serving_uuid, st["uid"])
        r = _gwt_post(body)
        r.raise_for_status()
        ok = r.text.startswith("//OK")
        return json.dumps({
            "serving_uuid":      serving_uuid,
            "ok":                ok,
            "response_starts":   r.text[:6],
            "response_first_400": r.text[:400],
        }, indent=2)
    except Exception as e:
        return f"remove_from_diary failed: {e}"


@mcp.tool()
def add_to_diary(food_id: int, amount: float, measure_id: int,
                  meal: str = "Uncategorized", date: str = "") -> str:
    """
    Add a food or recipe to Eddie's diary. Wire format fully verified
    against two captures (Uncategorized food + Breakfast recipe).

    Args:
      food_id     — Cronometer food id OR custom recipe id (look up via
                    search_foods or list_my_foods).
      amount      — grams for raw foods; serving count for recipes
                    (use 1 + the recipe's "Serving" measure_id).
      measure_id  — Cronometer measure id. For raw foods, the "g" measure
                    id from getFood. For recipes, the "Serving" or
                    "full recipe" measure id.
      meal        — one of "Uncategorized", "Breakfast", "Lunch",
                    "Dinner", "Snacks". Default Uncategorized.
      date        — YYYY-MM-DD; defaults to today.
    """
    date = date or _today()
    meal_id = _MEAL_ID.get(meal, 1)
    try:
        st = _get_state()
        y, mo, d = (int(x) for x in date.split("-"))
        body = _build_update_diary_add_body(
            st["sk"], st["uid"], food_id, amount, measure_id, meal,
            d, mo, y,
        )
        r = _gwt_post(body)
        r.raise_for_status()
        ok = r.text.startswith("//OK")
        return json.dumps({
            "food_id":           food_id,
            "amount":            amount,
            "measure_id":        measure_id,
            "meal":              meal,
            "meal_id_used":      meal_id,
            "date":              date,
            "ok":                ok,
            "response_starts":   r.text[:6],
            "response_first_500": r.text[:500],
        }, indent=2)
    except Exception as e:
        return f"add_to_diary failed: {e}"


# ── addFood (create custom recipe) ───────────────────────────────────────────
# Wire format reverse-engineered from two captured `addFood` requests
# ("Test" and "Test 2"; same 2-ingredient, 200g recipe, only the name and
# a trailing counter differ between the two). The string table has 36
# entries; the body inlines a Food object + IngredientSubstitutions.
#
# Decoded sections (positions in the body, after method dispatch + types +
# token-ref + uid-literal):
#   Food:
#     - Ingredient list (ArrayList): N ingredients, each
#         `12 | grams | food_id | A | measure_id | 0 | <hash_or_0>`
#       The trailing 7-digit number on the last ingredient looks like a
#       client-side hash (same value across both captures because the
#       recipe contents were identical). Hardcoded for now; if this 500s
#       on novel recipes we'll switch to 0 and iterate.
#     - 3 measures (full-recipe / Serving / g) — fixed structure.
#     - total_grams (literal int).
#     - NutrientMap with N nutrient entries; the first uses pattern
#       `23|<id>|24|<val>|<id>|25|0|` and subsequent use
#       `23|<id>|24|<val>|<id>|-18|`. Final entry transitions out.
#   IngredientSubstitutions: one HashMap entry "advancedServingSize"="false",
#     plus FoodTags (HashSet of size 1: "Custom"), plus an English
#     Translation block carrying the recipe name, plus FoodType ordinal.

# Total nutrient set Cronometer expects in the recipe map. Captured from
# Test/Test 2 wire — 89 entries covering every macro, micro, amino acid,
# fatty acid, and computed nutrient the UI displays. Fixed list keeps the
# wire shape stable; ingredients that don't report a nutrient default to 0.
_RECIPE_NUTRIENT_IDS = [
    -1205, 203, 204, 205, 207, 208, 209, 210, 211, 212, 213, 214, 221, 246,
    255, 262, 269, 287, 291, 295, 297, 301, 303, 304, 305, 306, 307, 309,
    312, 315, 317, 319, 320, 321, 322, 323, 324, 334, 337, 338, 341, 342,
    343, 401, 404, 405, 406, 410, 415, 417, 418, 421, 430, 501, 502, 503,
    504, 505, 506, 507, 508, 509, 510, 511, 512, 513, 514, 515, 516, 517,
    518, 601, 605, 606, 621, 629, 645, 646, 675, 851, 853, 10001, 10002,
    10007, 10009, 10012, -203, -205, -204, -221,
]

# Fixed string-table prefix for addFood — slots 1-34 + trailing slots 36
# match in every capture. Slot 35 is the recipe name (varies). Slot 11 is
# left empty as in the captures. The literal token + name go in by string
# substitution.

def _build_add_food_body(
    token: str, user_id: int, name: str,
    ingredients: list[tuple[int, float, int]],   # [(food_id, grams, measure_id)]
    total_nutrients: dict[int, float],
    counter: int = 1,
    draft_hash: int = 2369486,                    # client-side hash, captured value
) -> str:
    """Build a GWT body for addFood — creates a custom recipe in Cronometer.

    Args:
      token            – v2 sessionKey doubling as GWT token
      user_id          – Eddie's userId (8560953)
      name             – display name for the new recipe
      ingredients      – list of (food_id, grams, measure_id) tuples
      total_nutrients  – {nutrient_id: total_value_for_recipe}; missing IDs
                         default to 0. Should be in the canonical nutrient
                         set (use _build_recipe_nutrients() to compute).
      counter          – session-local recipe counter; safe to leave at 1
      draft_hash       – server-validated content hash; default is the value
                         captured for the "Test" recipe (chicken+rice 200g).
                         Will likely differ for other ingredient combos —
                         try 0 first, escalate if Cronometer rejects.
    """
    total_grams = sum(g for _, g, _ in ingredients)
    n_ing = len(ingredients)

    # ── string table (36 entries) ─────────────────────────────────────────
    # Slot 9 = token, slot 35 = recipe name. Everything else is fixed.
    table = "|".join([
        GWT_MODULE_BASE,
        POLICY,
        SVC,
        "addFood",
        STR_TYPE,                                                       # 5
        "I",                                                            # 6
        "com.cronometer.shared.foods.models.Food/2097636843",           # 7
        "com.cronometer.shared.foods.models.IngredientSubstitutions/1892525086",  # 8
        token,                                                          # 9
        "java.util.ArrayList/4159755760",                               # 10
        "",                                                             # 11 (empty)
        "com.cronometer.shared.foods.models.Ingredient/1280520736",     # 12
        "com.cronometer.shared.foods.NutritionLabelType/1598919019",    # 13
        "com.cronometer.shared.foods.models.FoodMeasures/2106205728",   # 14
        "com.cronometer.shared.foods.models.Measure/824760657",         # 15
        "full recipe",                                                  # 16
        "com.cronometer.shared.foods.models.Measure$Type/2365167904",   # 17
        "Serving",                                                      # 18
        "g",                                                            # 19
        "com.cronometer.shared.foods.models.NutrientMap/168231382",     # 20
        "com.cronometer.shared.foods.models.NutrientMap$NutrientFilter/1990310964",  # 21
        "java.util.HashMap/1797211028",                                 # 22
        "java.lang.Integer/3438268394",                                 # 23
        "com.cronometer.shared.foods.models.Nutrient/331784102",        # 24
        "com.cronometer.shared.foods.models.Nutrient$Type/4187872513",  # 25
        "advancedServingSize",                                          # 26
        "false",                                                        # 27
        "Custom",                                                       # 28
        "java.util.HashSet/3273092938",                                 # 29
        "com.cronometer.shared.foods.models.Translation/4034452093",    # 30
        "com.cronometer.shared.user.models.Language/1257207975",        # 31
        "en",                                                           # 32
        "English",                                                      # 33
        "https://cdn1.cronometer.com/media/flags/us.png",               # 34
        name,                                                           # 35
        "com.cronometer.shared.foods.FoodType/2323555378",              # 36
    ])

    # ── body section ──────────────────────────────────────────────────────
    parts = []
    # Method dispatch + 4 type refs + token ref + uid literal
    parts.append("1|2|3|4|4|5|6|7|8|9|" + str(user_id))

    # Food prelude — fixed in both captures
    parts.append("7|0|0|10|0|0|11|0|0|0|10|" + str(n_ing))

    # Each ingredient: 12|grams|food_id|A|measure_id|0|<hash_or_0>|
    # The hash slot is 0 for all but the LAST ingredient, which carries
    # the draft_hash (per the captures).
    for i, (food_id, grams, measure_id) in enumerate(ingredients):
        is_last = (i == n_ing - 1)
        trailer = str(draft_hash) if is_last else "0"
        parts.append(f"12|{int(grams)}|{food_id}|A|{measure_id}|0|{trailer}")

    # NutritionLabelType + FoodMeasures (3 measures: full recipe / Serving / g)
    parts.append("13|1|A|14|0|10|3|"
                  "15|1|0|0|0|0|16|17|3|1|"   # measure 1: "full recipe"
                  "15|1|0|0|0|0|18|-10|1|"    # measure 2: "Serving"
                  "15|1|0|0|0|0|19|-10|"      # measure 3: "g"
                  + str(int(total_grams)))

    # NutrientMap — first entry uses one pattern, rest use another. The
    # count Cronometer expects is the literal entry count (verified by
    # diff against the captured Test 2 body).
    parts.append("20|21|0|22|" + str(len(_RECIPE_NUTRIENT_IDS)))
    for idx, nid in enumerate(_RECIPE_NUTRIENT_IDS):
        val = total_nutrients.get(nid, 0)
        # Cronometer accepts ints for whole numbers; floats otherwise
        v_str = (str(int(val)) if isinstance(val, (int, float)) and val == int(val)
                  else f"{val}")
        if idx == 0:
            # First entry uses |25|0| terminator instead of |-18|
            parts.append(f"23|{nid}|24|{v_str}|{nid}|25|0")
        else:
            parts.append(f"23|{nid}|24|{v_str}|{nid}|-18")

    # IngredientSubstitutions: HashMap("advancedServingSize"="false") +
    # HashSet("Custom") + Translation + FoodType + counter
    parts.append("22|1|5|26|5|27|0|"            # HashMap with 1 String entry
                  "28|29|0|10|1|"               # HashSet "Custom" with 1 ArrayList
                  "30|31|32|33|34|33|35|0|"     # Translation: en/English/flag/recipe-name
                  "36|" + str(counter) + "|"
                  + str(user_id) + "|0")

    body_section = "|".join(parts) + "|"
    string_table_count = len(table.split("|"))

    return f"7|0|{string_table_count}|{table}|{body_section}"


def _build_recipe_nutrients(ingredients: list[tuple[int, float, int]]) -> dict[int, float]:
    """Compute the recipe's TOTAL nutrient values by summing each ingredient's
    per-100g nutrition × (grams/100), across the canonical recipe nutrient
    set. Calls _fetch_food (cached) for each ingredient.

    Returns a dict keyed by Cronometer nutrient id, with whole-number
    values rounded to 3 decimals to match what the UI sends on the wire.
    """
    totals: dict[int, float] = {nid: 0.0 for nid in _RECIPE_NUTRIENT_IDS}
    for food_id, grams, _measure in ingredients:
        food = _fetch_food(food_id)
        per100 = food["nutrients_per_100g"]
        scale  = grams / 100.0
        for nid in _RECIPE_NUTRIENT_IDS:
            totals[nid] += per100.get(nid, 0.0) * scale
    return {nid: round(v, 3) for nid, v in totals.items()}


@mcp.tool()
def dry_run_create_recipe(name: str, ingredients_json: str,
                           draft_hash: int = 0) -> str:
    """
    Build the addFood request body for a new recipe WITHOUT sending it.
    Use this to eyeball the wire shape before live-firing create_recipe.

    Args:
      name             – display name
      ingredients_json – JSON array of [food_id, grams, measure_id] triples,
                         e.g. '[[462802, 100, 1061340], [460334, 100, 1048861]]'
      draft_hash       – defaults to 0 (untested; the captured value 2369486
                         worked for chicken-rice but is content-dependent)
    """
    try:
        ings = json.loads(ingredients_json)
        ings = [(int(f), float(g), int(m)) for f, g, m in ings]
        st = _get_state()
        nutrients = _build_recipe_nutrients(ings)
        body = _build_add_food_body(
            st["sk"], st["uid"], name, ings, nutrients,
            counter=1, draft_hash=draft_hash,
        )
        return json.dumps({
            "name":           name,
            "ingredients":    [{"food_id": f, "grams": g, "measure_id": m}
                                for f, g, m in ings],
            "total_grams":    sum(g for _, g, _ in ings),
            "computed_macros": {
                "calories":  round(nutrients.get(208, 0), 1),
                "protein_g": round(nutrients.get(203, 0), 2),
                "fat_g":     round(nutrients.get(204, 0), 2),
                "carbs_g":   round(nutrients.get(205, 0), 2),
                "fiber_g":   round(nutrients.get(291, 0), 2),
            },
            "body_length":    len(body),
            "body":           body,
        }, indent=2)
    except Exception as e:
        return f"dry_run_create_recipe failed: {e}"


@mcp.tool()
def create_recipe(name: str, ingredients_json: str,
                   draft_hash: int = 0) -> str:
    """
    EXPERIMENTAL — create a custom recipe in Cronometer via addFood.
    Aggregates ingredient nutrition automatically from each food's per-100g
    data. Run dry_run_create_recipe FIRST to preview the body.

    Args:
      name             – display name for the recipe
      ingredients_json – JSON array of [food_id, grams, measure_id] triples
      draft_hash       – content-dependent client hash. Defaults to 0; if
                         Cronometer 500s, try the captured value 2369486
                         (only valid for the chicken-rice combo).
    """
    try:
        ings = json.loads(ingredients_json)
        ings = [(int(f), float(g), int(m)) for f, g, m in ings]
        st = _get_state()
        nutrients = _build_recipe_nutrients(ings)
        body = _build_add_food_body(
            st["sk"], st["uid"], name, ings, nutrients,
            counter=1, draft_hash=draft_hash,
        )
        r = _gwt_post(body)
        r.raise_for_status()
        ok = r.text.startswith("//OK")
        return json.dumps({
            "name":              name,
            "ok":                ok,
            "response_starts":   r.text[:6],
            "response_first_500": r.text[:500],
            "body_length":       len(body),
        }, indent=2)
    except Exception as e:
        return f"create_recipe failed: {e}"


if __name__ == "__main__":
    mcp.run()
