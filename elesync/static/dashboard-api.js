/* Shared dashboard transport and loader primitives.

   This file intentionally remains a classic script: the dashboard still uses
   inline actions and requires no frontend build step. Feature files can share
   these globals while the packaged app stays stdlib-only.
*/
/* Phosphor icon from the page's inline sprite (elesync/_icons.py). Names are
   code constants, never user input. */
function icon(name) {
  return '<svg class="ph" aria-hidden="true" focusable="false"><use href="#ph-' + name + '"/></svg>';
}
async function api(path, opts = {}) {
  const { timeoutMs, signal: extSignal, ...fetchOpts } = opts;
  let ctrl, timer, onExtAbort;
  let timedOut = false;
  if (timeoutMs || extSignal) {
    ctrl = new AbortController();
    fetchOpts.signal = ctrl.signal;
    if (timeoutMs) {
      timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, timeoutMs);
    }
    if (extSignal) {
      if (extSignal.aborted) ctrl.abort();
      else {
        onExtAbort = () => ctrl.abort();
        extSignal.addEventListener('abort', onExtAbort, { once: true });
      }
    }
  }
  let res;
  try {
    res = await fetch(path, fetchOpts);
  } catch (e) {
    if (e.name === 'AbortError') {
      if (timedOut) throw new Error('Request timed out');
      const sup = new Error('Superseded');
      sup.name = 'AbortError';
      throw sup;
    }
    const offline = new Error('Network error: ' + (e.message || e));
    offline.network = true; // the request never reached EleSync
    throw offline;
  } finally {
    if (timer) clearTimeout(timer);
    if (extSignal && onExtAbort) extSignal.removeEventListener('abort', onExtAbort);
  }
  if (res.status === 401) {
    window.location.href = '/login';
    const signedOut = new Error('Auth required');
    signedOut.status = 401;
    throw signedOut;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    const refused = new Error(err.error || res.statusText);
    refused.status = res.status;
    throw refused;
  }
  return res.json();
}

function skel(widths) {
  return widths.map(function (w) {
    return '<div class="skel" style="width:' + w + '%"></div>';
  }).join('');
}

const _lanes = Object.create(null);

async function withLoader(el, widths, label, run, lane) {
  let signal;
  if (lane) {
    const prev = _lanes[lane];
    if (prev) prev.abort();
    const ctrl = new AbortController();
    _lanes[lane] = ctrl;
    signal = ctrl.signal;
  }
  el.innerHTML = skel(widths);
  try {
    await run(signal);
  } catch (e) {
    if (e.name === 'AbortError') return;
    el.innerHTML = '<div class="empty-state compact"><div class="empty-text text-danger">' +
      _gEsc(label) + ' failed: ' + _gEsc(e.message) + '</div></div>';
  } finally {
    if (lane && _lanes[lane] && _lanes[lane].signal === signal) delete _lanes[lane];
  }
}
