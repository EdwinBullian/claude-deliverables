/* Shared force-refresh control for the health widgets.
 *
 * Each widget includes this with two globals set first, e.g.:
 *   <script>window.REFRESH_SECTIONS='sleep,energy'; window.REFRESH_FORCE_ZEPP=true;</script>
 *   <script src="./refresh.js"></script>
 *
 * Behavior:
 *  - Injects a small ⟳ button (top-right).
 *  - Reads the trigger endpoint from data.json's _meta.refresh_endpoint (set by
 *    the pipeline from the REFRESH_ENDPOINT repo variable → the Cloudflare Worker).
 *  - With an endpoint: POSTs {sections, force_zepp} to the Worker (which dispatches
 *    the GitHub Action), then polls data.json until _meta.last_updated_iso advances
 *    and reloads. Without one: just reloads to re-pull the latest data.json (soft).
 */
(function () {
  var SECTIONS = window.REFRESH_SECTIONS || 'full';
  var FORCE = !!window.REFRESH_FORCE_ZEPP;
  var DATA_URL = './data/data.json';
  var POLL_MS = 4000;
  var TIMEOUT_MS = 150000;

  var css = document.createElement('style');
  css.textContent =
    '.cw-refresh{position:fixed;top:6px;right:7px;z-index:9999;width:22px;height:22px;border:none;' +
    'border-radius:6px;cursor:pointer;background:rgba(255,255,255,.06);color:rgba(255,255,255,.5);' +
    'display:flex;align-items:center;justify-content:center;padding:0;-webkit-tap-highlight-color:transparent;' +
    'transition:background .15s,color .15s}' +
    '.cw-refresh:hover{background:rgba(255,255,255,.13);color:rgba(255,255,255,.85)}' +
    '.cw-refresh svg{width:13px;height:13px;display:block}' +
    '.cw-refresh.busy{pointer-events:none;color:#7cc0f0}' +
    '.cw-refresh.busy svg{animation:cwspin .8s linear infinite}' +
    '.cw-refresh.ok{color:#6bda87}.cw-refresh.err{color:#e88}' +
    '@keyframes cwspin{to{transform:rotate(360deg)}}' +
    '.cw-toast{position:fixed;top:6px;right:34px;z-index:9999;font:600 9px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;' +
    'color:rgba(255,255,255,.72);background:rgba(20,20,20,.88);padding:4px 8px;border-radius:6px;opacity:0;' +
    'transition:opacity .2s;pointer-events:none;white-space:nowrap}.cw-toast.show{opacity:1}';
  document.head.appendChild(css);

  var btn = document.createElement('button');
  btn.className = 'cw-refresh';
  btn.title = 'Force refresh';
  btn.setAttribute('aria-label', 'Force refresh');
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/>' +
    '<path d="M21 3v6h-6"/></svg>';
  document.body.appendChild(btn);

  var toast = document.createElement('div');
  toast.className = 'cw-toast';
  document.body.appendChild(toast);
  function say(m) { toast.textContent = m; toast.classList.add('show'); }
  function hideToast() { toast.classList.remove('show'); }

  async function readMeta() {
    try {
      var r = await fetch(DATA_URL + '?t=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) return {};
      var j = await r.json();
      return j._meta || {};
    } catch (e) { return {}; }
  }

  btn.addEventListener('click', async function () {
    btn.className = 'cw-refresh busy';
    var meta = await readMeta();
    var endpoint = meta.refresh_endpoint || '';
    var before = meta.last_updated_iso || '';

    if (!endpoint) {
      // No proxy configured yet — soft refresh (re-pull the latest data.json).
      say('Reloading…');
      setTimeout(function () { location.reload(); }, 350);
      return;
    }

    say('Refreshing…');
    try {
      await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sections: SECTIONS, force_zepp: FORCE })
      });
    } catch (e) { /* opaque/CORS errors on the trigger are fine — poll anyway */ }

    var t0 = Date.now();
    (function poll() {
      if (Date.now() - t0 > TIMEOUT_MS) {
        btn.className = 'cw-refresh err';
        say('Timed out — try again');
        setTimeout(hideToast, 2600);
        setTimeout(function () { btn.className = 'cw-refresh'; }, 2600);
        return;
      }
      readMeta().then(function (m) {
        if (m.last_updated_iso && m.last_updated_iso !== before) {
          btn.className = 'cw-refresh ok';
          say('Updated ✓');
          setTimeout(function () { location.reload(); }, 700);
        } else {
          setTimeout(poll, POLL_MS);
        }
      });
    })();
  });
})();
