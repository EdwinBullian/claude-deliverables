# Force-refresh proxy — 5-minute deploy

This Cloudflare Worker lets the dashboard's ⟳ buttons trigger a real pipeline
refresh without exposing any token in the public widget code.

## 1. Create the Worker
1. Go to **dash.cloudflare.com → Workers & Pages → Create → Create Worker**.
2. Name it something like `health-refresh`. Click **Deploy** (the default code is fine for now).
3. Click **Edit code**, delete the template, paste the contents of `worker.js`, then **Deploy**.

## 2. Add the GitHub token (kept server-side)
1. Create a **fine-grained** GitHub token: **github.com → Settings → Developer settings →
   Fine-grained tokens → Generate new token**.
   - **Resource owner:** your account
   - **Repository access:** Only select repositories → `EdwinBullian/claude-deliverables`
   - **Permissions:** Repository permissions → **Actions: Read and write** (nothing else)
   - Generate and copy it.
2. In the Worker: **Settings → Variables and Secrets → Add → Secret**
   - Name: `GH_TOKEN`
   - Value: paste the token → **Save and deploy**.

> Why fine-grained + Actions-only: even in the unlikely event the Worker URL leaks,
> that token can do nothing but trigger/read Actions on this one repo — it can't read
> code, secrets, or anything else.

## 3. Point the widgets at the Worker
1. Copy the Worker URL (e.g. `https://health-refresh.<you>.workers.dev`).
2. In the repo: **Settings → Secrets and variables → Actions → Variables tab → New repository variable**
   - Name: `REFRESH_ENDPOINT`
   - Value: the Worker URL
3. Trigger one refresh (tap any ⟳ button, or run the Action once). From then on the
   widgets read the endpoint from `data.json` automatically — no widget redeploy needed.

That's it. Until `REFRESH_ENDPOINT` is set, the ⟳ buttons simply reload the latest
data (safe). Once it's set, they trigger a live pull from Zepp / Cronometer / Notion.

## How each button behaves
- **Sleep / Energy / Activity / Training** → forces a Zepp re-pull (overrides the
  once-a-day guard, so it works even if you woke earlier than the scheduled pull).
- **Macros / Micros / Hydration** → refreshes Cronometer only (won't log your phone
  out of Zepp).
- **Supplements** → refreshes Notion only.

A tap shows a spinner, waits for the pipeline to finish (~20–60s), then reloads with
fresh numbers. If it can't confirm an update within ~2.5 min it shows "timed out — try again".
