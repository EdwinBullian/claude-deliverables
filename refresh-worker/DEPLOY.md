# Force-refresh proxy — deploy guide

The dashboard's ⟳ buttons trigger a real pipeline refresh without exposing any
token in the public widget code. Three one-time steps below. Until they're done,
the buttons simply reload the latest data (safe) — they "upgrade" to a true
source-pull automatically once configured.

---

## 0. Add the workflow inputs (one file I couldn't push)

Everything else is already live in the repo. The only piece left is two new
inputs on the Action — and the API token I use lacks the GitHub **`workflow`**
scope, so GitHub blocks me from editing files under `.github/workflows/`.

**Easiest fix:** GitHub → your token → tick the **`workflow`** checkbox (classic
PAT: *Settings → Developer settings → Tokens → edit → check `workflow`*), tell me,
and I'll push it in one shot.

**Or do it yourself** in the GitHub web editor (the web UI isn't scope-limited).
Edit `.github/workflows/refresh-health-data.yml`:

**(a)** Under `workflow_dispatch: inputs:` — right after the `dry_run` block — add:

```yaml
      sections:
        description: 'Sections preset (full/fast/nutrition) or comma list'
        required: false
        type: string
        default: 'full'
      force_zepp:
        description: 'Force a Zepp re-pull (override the once/day guard)'
        required: false
        type: string
        default: 'false'
```

**(b)** In the **Build data.json** step, add two env vars after
`NOTION_SUPPLEMENT_DB_ID:` …

```yaml
          REFRESH_ENDPOINT:         ${{ vars.REFRESH_ENDPOINT }}
          FORCE_ZEPP:               ${{ (github.event_name == 'workflow_dispatch' && inputs.force_zepp == 'true') && '1' || '' }}
```

…and change the run line to pass the sections through:

```yaml
        run: |
          python scripts/build_data.py health/widgets/data/data.json --sections "${{ (github.event_name == 'workflow_dispatch' && inputs.sections) || 'full' }}"
```

(Scheduled runs ignore the inputs and keep doing a normal full refresh.)

---

## 1. Create the Worker
1. **dash.cloudflare.com → Workers & Pages → Create → Create Worker**.
2. Name it e.g. `health-refresh`. **Deploy**, then **Edit code**, paste `worker.js`, **Deploy**.

## 2. Add the GitHub token (kept server-side)
1. Create a **fine-grained** token (github.com → Settings → Developer settings →
   Fine-grained tokens → Generate):
   - **Repository access:** Only `EdwinBullian/claude-deliverables`
   - **Permissions:** **Actions: Read and write** (nothing else)
2. Worker → **Settings → Variables and Secrets → Add → Secret**
   - Name `GH_TOKEN`, value = the token → **Save and deploy**.

> Even if the Worker URL leaked, this token can only trigger/read Actions on this
> one repo — no code, no secrets, nothing else.

## 3. Point the widgets at the Worker
1. Copy the Worker URL (e.g. `https://health-refresh.<you>.workers.dev`).
2. Repo → **Settings → Secrets and variables → Actions → Variables tab → New variable**
   - Name `REFRESH_ENDPOINT`, value = the Worker URL.
3. Tap any ⟳ button once (or run the Action). From then on the widgets read the
   endpoint from `data.json` automatically — no widget redeploy needed.

---

## How each button behaves
- **Sleep / Energy / Activity / Training** → forces a Zepp re-pull (overrides the
  once/day guard, so it works even if you woke earlier than the scheduled pull).
- **Macros / Micros / Hydration** → Cronometer only (won't log your phone out of Zepp).
- **Supplements** → Notion only.

A tap shows a spinner, waits for the pipeline (~20–60s), then reloads with fresh
numbers. If it can't confirm an update within ~2.5 min it shows "timed out — try again".
