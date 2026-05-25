# Health Data Pipeline — Setup

This adds an automated pipeline that refreshes your Notion health widgets every 6 hours, running entirely on **GitHub Actions** so it works whether or not your computer is on.

```
GitHub Actions (cron every 6h)
   ├─► scripts/fetch_zepp.py       → sleep, activity, HR
   ├─► scripts/fetch_cronometer.py → macros, micros
   ├─► scripts/fetch_notion.py     → workouts, supplements
   └─► scripts/build_data.py       → merges everything
                                       │
                                       ▼
                          health/widgets/data/data.json
                                       │
                          (committed back to main)
                                       │
                                       ▼
                          GitHub Pages serves the file
                                       │
                                       ▼
                Each widget HTML fetch()es it on load
```

---

## One-time setup

### 1 · Enable GitHub Pages

`Settings → Pages` → **Source: Deploy from a branch** → **Branch: main / (root)** → Save.

Your widget URLs become:

```
https://edwinbullian.github.io/claude-deliverables/health/widgets/sleep-ring.html
https://edwinbullian.github.io/claude-deliverables/health/widgets/activity-biometrics.html
…etc
```

Embed those URLs in Notion (`/embed` block, paste URL).

### 2 · Add GitHub secrets

`Settings → Secrets and variables → Actions → New repository secret`. Add:

| Secret | Where to get it |
|---|---|
| `ZEPP_EMAIL` | Your Zepp app login |
| `ZEPP_PASSWORD` | Your Zepp app password |
| `ZEPP_APP_TOKEN` | *(optional)* If login keeps 0100-erroring, capture this from your phone — see "Zepp auth fallback" below |
| `ZEPP_USER_ID` | *(optional, paired with `ZEPP_APP_TOKEN`)* |
| `CRONOMETER_EMAIL` | Your Cronometer login |
| `CRONOMETER_PASSWORD` | Your Cronometer password |
| `NOTION_TOKEN` | https://www.notion.so/profile/integrations → New integration → copy the **internal integration secret** |
| `NOTION_WORKOUT_DB_ID` | Open your workout tracker DB in Notion → URL like `…/<32-char-id>?v=…` → that 32-char string (with or without dashes) |
| `NOTION_SUPPLEMENT_DB_ID` | Same as above for your supplements DB (optional — without it the widget keeps its current static list) |

**Don't forget**: share each Notion database with your integration ("…" menu in the database → Connections → add your integration), otherwise the API returns 404.

### 3 · *(optional)* Repository variables for nutrition targets

`Settings → Secrets and variables → Actions → Variables tab`. Add:

| Variable | Default | Notes |
|---|---|---|
| `CRONOMETER_TARGET_KCAL` | 2500 | Daily calorie target |
| `CRONOMETER_TARGET_PROTEIN_G` | 200 | Daily protein target |

### 4 · Trigger the first run

`Actions → Refresh Health Data → Run workflow`. Watch the logs — every source reports `OK` or `ERR :: <reason>` so you can see exactly which credential needs fixing.

Once it succeeds, `health/widgets/data/data.json` will be committed to `main` and your widgets will pick it up on next load.

---

## File layout

```
.github/workflows/refresh-health-data.yml   ← cron + commit job
scripts/
  common.py                                  ← shared helpers (time, env, fallback merge)
  fetch_zepp.py                              ← Huami auth flow + band/sleep/workout pull
  fetch_cronometer.py                        ← cronometer.com session login + diary pull
  fetch_notion.py                            ← official Notion API for workouts + supplements
  build_data.py                              ← orchestrator: runs fetchers in parallel,
                                                preserves previous values when a source fails
  requirements.txt
health/widgets/
  sleep-ring.html
  activity-biometrics.html
  energy-curve.html
  macro-trend.html
  micro-rings.html
  strength-trend.html
  supp-stack.html
  data/
    data.json            ← live data (committed by the Action)
    data.example.json    ← shape reference for future schema edits
```

---

## Notion workout-tracker schema (what the fetcher expects)

The Notion fetcher matches property names case-insensitively and accepts any of these names per field:

| Field | Property type | Aliases |
|---|---|---|
| Date of session | `date` | `Date`, `Workout Date`, `Day` |
| Split | `select` | `Type`, `Category`, `Split` |
| Length | `number` | `Duration`, `Min`, `Minutes`, `Length` |
| Top lifts | `rich_text` | `Top Lifts`, `Lifts`, `Notes` |

The "Top Lifts" field is pipe-separated, one lift per line:

```
Incline DB Press|55 lb × 8|→ try 60
Overhead Press|35 lb × 12|→ try 40
DB Rows|35 lb × 12|→ try 40
Bicep Curls|15 lb × 12|→ try 20
```

The first three lifts of the most recent session populate the strength widget.

For supplements (optional DB), expected properties: `Name`, `Description`, `Dose`, `Timing`.

---

## Zepp auth fallback (if email/password login keeps failing)

The Zepp/Huami cloud sometimes rejects fresh logins with `error_code: 0100`. If the workflow logs show that:

1. On your phone, set up mitmproxy or use the open-source [huami-token](https://github.com/argrento/huami-token) tool to capture the `app_token` and `user_id` from a logged-in Zepp app session.
2. Add `ZEPP_APP_TOKEN` and `ZEPP_USER_ID` as repo secrets.
3. The fetcher will detect them and skip the email/password flow.

These tokens last weeks-to-months before needing refresh.

---

## Failure mode behavior

- **One source fails** → workflow continues; widgets keep showing the previous values for that source's sections. The `_meta.sources.<src>.error` field in `data.json` shows why, and the workflow log prints `ERR :: <reason>`.
- **All sources fail** → workflow exits non-zero so GitHub emails you. Widgets keep their last-known-good `data.json` from the previous successful run.
- **Network error in the browser** → widget falls back to the in-file `FALLBACK = {...}` constants so it always renders something.

---

## Local testing

```bash
cd <repo>
pip install -r scripts/requirements.txt

export ZEPP_EMAIL=...     # all the secrets listed above
export NOTION_TOKEN=...
# etc.

python scripts/build_data.py /tmp/data.json
cat /tmp/data.json | jq '._meta.sources'
```

---

## Adjusting cadence

The cron line in `.github/workflows/refresh-health-data.yml`:

```yaml
- cron: '0 1,7,13,19 * * *'   # 06:00, 12:00, 18:00, 00:00 PST
```

Daily-only would be `0 13 * * *` (6 AM PST). Hourly is `0 * * * *`. Free-tier GitHub Actions on a private repo gives 2,000 minutes/month; this job uses ~30s per run, so even hourly is well within budget.
