/* Cloudflare Worker — health dashboard force-refresh proxy.
 *
 * Holds the GitHub token server-side so the public widgets never expose it.
 * The widgets POST here; this dispatches the refresh-health-data GitHub Action.
 *
 * Setup (see DEPLOY.md):
 *   1. Create the Worker, paste this code.
 *   2. Add a secret named GH_TOKEN  (fine-grained PAT: repo
 *      EdwinBullian/claude-deliverables, permission "Actions: Read and write").
 *   3. Copy the Worker URL (e.g. https://health-refresh.<you>.workers.dev) and set
 *      it as the repo Actions VARIABLE  REFRESH_ENDPOINT  so the widgets pick it up.
 */

const REPO = 'EdwinBullian/claude-deliverables';
const WORKFLOW = 'refresh-health-data.yml';
const ALLOW_ORIGIN = 'https://edwinbullian.github.io';

export default {
  async fetch(req, env) {
    const cors = {
      'Access-Control-Allow-Origin': ALLOW_ORIGIN,
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    if (req.method !== 'POST') {
      return new Response('POST only', { status: 405, headers: cors });
    }

    let body = {};
    try { body = await req.json(); } catch (e) { /* empty body = full refresh */ }

    // Whitelist the sections value to a safe character set.
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
    const detail = ok ? '' : await resp.text().catch(() => '');
    return new Response(JSON.stringify({ ok, github_status: resp.status, detail }), {
      status: ok ? 200 : 502,
      headers: { ...cors, 'Content-Type': 'application/json' },
    });
  },
};
