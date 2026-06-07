/* Cloudflare Worker — health dashboard force-refresh proxy (rate-limited).
 *
 * Holds the GitHub token server-side so the public widgets never expose it.
 * The widgets POST here; this dispatches the refresh-health-data GitHub Action.
 * A short per-edge cooldown stops the trigger from being spammed even if the
 * Worker URL becomes known (it ends up in the public widget code).
 *
 * Secret required: GH_TOKEN  (GitHub token that can dispatch Actions on the repo).
 */

const REPO = 'EdwinBullian/claude-deliverables';
const WORKFLOW = 'refresh-health-data.yml';
const ALLOW_ORIGIN = 'https://edwinbullian.github.io';
const RATE_LIMIT_SECONDS = 90;

export default {
  async fetch(req, env) {
    const cors = {
      'Access-Control-Allow-Origin': ALLOW_ORIGIN,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    if (req.method !== 'POST') return new Response('POST only', { status: 405, headers: cors });

    // --- per-edge cooldown: reject if we triggered within the last window ---
    const cache = caches.default;
    const lockKey = new Request('https://rl.internal/health-refresh-lock');
    if (await cache.match(lockKey)) {
      return new Response(
        JSON.stringify({ ok: false, error: 'rate_limited', retry_after_s: RATE_LIMIT_SECONDS }),
        { status: 429, headers: { ...cors, 'Content-Type': 'application/json' } }
      );
    }

    let body = {};
    try { body = await req.json(); } catch (e) { /* empty body = full refresh */ }

    let sections = typeof body.sections === 'string' ? body.sections : 'full';
    if (!/^[a-z_,]{1,120}$/.test(sections)) sections = 'full';
    const force_zepp = body.force_zepp === true || body.force_zepp === 'true' ? 'true' : 'false';

    const resp = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `token ${env.GH_TOKEN}`,
          'Accept': 'application/vnd.github+json',
          'User-Agent': 'health-refresh-worker',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref: 'main', inputs: { sections, force_zepp } }),
      }
    );

    const ok = resp.status === 204; // GitHub returns 204 No Content on success
    if (ok) {
      // start the cooldown only after a successful trigger
      await cache.put(
        lockKey,
        new Response('1', { headers: { 'Cache-Control': `max-age=${RATE_LIMIT_SECONDS}` } })
      );
    }
    const detail = ok ? '' : await resp.text().catch(() => '');
    return new Response(JSON.stringify({ ok, github_status: resp.status, detail }), {
      status: ok ? 200 : 502,
      headers: { ...cors, 'Content-Type': 'application/json' },
    });
  },
};
