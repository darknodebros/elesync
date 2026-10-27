"""Behavioural tests for the dashboard's request-cancellation logic.

These do not re-implement the code under test: they extract the *real* shipped
`api`, `skel`, `withLoader`, `_gEsc` and `doSearch` from the served `_HTML`
script, stub only the browser (document / fetch), and execute them under node.

The bug they exist to prevent:

    Type "ele"      -> debounce fires -> request A goes out, and is slow.
    Type "elephant" -> debounce fires -> request B goes out, and returns first.
    B renders "elephant" results. Then A lands and overwrites the DOM with
    results for "ele".

Debouncing does not prevent this — it only shrinks the window. The user is left
looking at results for a query they already finished editing, with no indication
anything is wrong.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

NEEDED = ("api", "skel", "withLoader", "_gEsc", "doSearch")


def _served_js() -> str:
    import elesync.web as web

    static = Path(web.__file__).parent / "static"
    return "\n\n".join(
        (static / name).read_text(encoding="utf-8") for name in ("dashboard-api.js", "dashboard.js")
    )


def _extract(js: str, name: str) -> str:
    """Pull one top-level function's source out of the served script, verbatim."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    if not m:
        raise AssertionError(f"function {name}() not found in the served dashboard JS")
    start = m.start()
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


class TestSearchRequestCancellation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        cls.js = _served_js()

    def _harness(self, body: str) -> dict:
        """Run the real dashboard functions under node with a stubbed browser."""
        js = self.js
        real = "\n\n".join(_extract(js, n) for n in NEEDED)

        # NOTE: `_lanes` is declared at module level in the real script, next to
        # withLoader rather than inside it, so extracting the functions alone
        # leaves it undefined. Declare it in the harness preamble instead of
        # asserting on its presence here — asserting would make these tests fail
        # on a missing *symbol* rather than on the stale render itself, which is
        # the thing that actually matters. Pre-fix code runs fine against this
        # preamble and fails on the behaviour, which is exactly what we want.

        program = (
            textwrap.dedent("""
            const _lanes = Object.create(null);

            // ---- minimal DOM ----
            const nodes = {
              'search-q':       { value: '' },
              'search-results': { innerHTML: '' },
            };
            global.document = { getElementById: (id) => nodes[id] };

            // Rendered by the real doSearch on a non-empty result set.
            function renderMemList(containerId, items) {
              nodes[containerId].innerHTML = 'RESULTS:' + items.map(i => i.q).join(',');
            }

            // ---- fetch stub: resolves after `delay`, honours AbortSignal ----
            let PLAN = {};
            const started = [];
            const aborted = [];
            global.fetch = (path, opts = {}) => {
              const q = decodeURIComponent((path.match(/q=([^&]*)/) || [])[1] || '');
              started.push(q);
              const delay = PLAN[q];
              return new Promise((resolve, reject) => {
                const timer = setTimeout(() => resolve({
                  ok: true, status: 200,
                  json: async () => ({ items: [{ q }], total: 1 }),
                }), delay);
                if (opts.signal) {
                  if (opts.signal.aborted) { clearTimeout(timer); }
                  opts.signal.addEventListener('abort', () => {
                    clearTimeout(timer);
                    aborted.push(q);
                    const e = new Error('aborted');
                    e.name = 'AbortError';
                    reject(e);
                  });
                }
              });
            };
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            """)
            + "\n\n"
            + real
            + "\n\n"
            + textwrap.dedent(body)
        )

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.mjs"
            f.write_text(program, encoding="utf-8")
            proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            self.fail(f"node harness failed:\n{proc.stderr}\n{proc.stdout}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_slow_first_query_cannot_overwrite_a_faster_later_one(self) -> None:
        """The exact race: 'ele' is slow, 'elephant' is fast and issued second."""
        out = self._harness("""
            PLAN = { ele: 200, elephant: 10 };

            (async () => {
              nodes['search-q'].value = 'ele';
              const first = doSearch();          // slow
              await sleep(5);
              nodes['search-q'].value = 'elephant';
              const second = doSearch();         // fast, issued second
              await Promise.all([first, second]);
              await sleep(250);                  // long enough for 'ele' to have landed
              console.log(JSON.stringify({
                html: nodes['search-results'].innerHTML,
                started, aborted,
              }));
            })();
        """)

        self.assertEqual(
            out["started"],
            ["ele", "elephant"],
            "both requests should have been issued",
        )
        self.assertIn(
            "ele",
            out["aborted"],
            "the superseded 'ele' request was never cancelled — it is still in "
            "flight and free to overwrite the DOM",
        )
        self.assertEqual(
            out["html"],
            "RESULTS:elephant",
            f"stale render: the DOM shows {out['html']!r}. A slower earlier query "
            f"overwrote the results of the query the user actually finished typing.",
        )

    def test_superseded_request_does_not_paint_an_error(self) -> None:
        """Cancelling is not failing.

        withLoader turns a thrown error into a red 'Search failed: ...' panel. An
        aborted request throws too — so if the abort is not distinguished from a
        real failure, superseding a query flashes a bogus error over the new
        results.
        """
        out = self._harness("""
            PLAN = { ele: 200, elephant: 10 };

            (async () => {
              nodes['search-q'].value = 'ele';
              const first = doSearch();
              await sleep(5);
              nodes['search-q'].value = 'elephant';
              const second = doSearch();
              await Promise.all([first, second]);
              await sleep(250);
              console.log(JSON.stringify({ html: nodes['search-results'].innerHTML }));
            })();
        """)
        self.assertNotIn(
            "failed",
            out["html"].lower(),
            f"a cancelled request was reported to the user as a failure: {out['html']!r}",
        )

    def test_a_real_failure_is_still_surfaced(self) -> None:
        """Guard against 'fix' by swallowing every error. Genuine errors must show."""
        out = self._harness("""
            global.fetch = () => Promise.reject(new TypeError('boom'));

            (async () => {
              nodes['search-q'].value = 'ele';
              await doSearch();
              await sleep(20);
              console.log(JSON.stringify({ html: nodes['search-results'].innerHTML }));
            })();
        """)
        self.assertIn(
            "failed",
            out["html"].lower(),
            f"a real network error was swallowed instead of shown: {out['html']!r}",
        )


if __name__ == "__main__":
    unittest.main()
