"""Escaping and event-binding guards for the dashboard.

The bug these exist to prevent:

A USB drive's `path` and `label` are derived from the *volume name* of whatever
stick is plugged in (``usb.py``: ``label = Path(mount_point).name``). That string
is attacker-controlled — you did not name the drive, whoever handed it to you
did. It was being interpolated:

  * into a single-quoted JS string inside an ``onclick`` attribute, via ``esc()``,
    which escapes ``&``/``<``/``>`` but NEITHER quote character. A volume named
    ``x'); alert(1); //`` broke straight out of the string.
  * raw, with no escaping at all, into ``innerHTML`` in ``usbSelect``'s status
    table (``${k}`` / ``${v}``), where ``drive`` and ``vault_dir`` are built from
    the mount path.

The dashboard has full read/write/export authority over the vault, so this was
total compromise from plugging in someone else's USB stick — which is a
first-class use case for the USB-vault feature, not an exotic one.

The fix was to remove the class rather than escape harder: user data now rides
in ``data-*`` attributes and is read back through ``.dataset``, so it never
enters an executable context.
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


def _served_js() -> str:
    import elesync.web as web

    static = Path(web.__file__).parent / "static"
    return "\n\n".join(
        (static / name).read_text(encoding="utf-8") for name in ("dashboard-api.js", "dashboard.js")
    )


def _served_html() -> str:
    """The served dashboard markup with its external script inlined.

    The dashboard JS now lives in ``static/dashboard.js`` rather than in the
    ``_HTML`` string, but the escaping guards below scan the *served document*
    for JS-generated markup (``onclick`` handlers, ``${esc(...)}`` interpolations).
    Inlining the script back in keeps those guards effective against the code
    that actually ships — otherwise they would go vacuously green.
    """
    import elesync.web as web

    return web._HTML.replace(
        '<script src="/static/dashboard.js"></script>',
        "<script>\n" + _served_js() + "\n</script>",
    )


def _extract(js: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", js)
    if not m:
        raise AssertionError(f"function {name}() not found in the served dashboard JS")
    start, i, depth = m.start(), m.end() - 1, 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


class TestNoDataInExecutableContext(unittest.TestCase):
    def test_no_onclick_interpolates_into_a_quoted_js_string(self) -> None:
        """``onclick="fn('${value}')"`` is the whole bug class.

        Any value interpolated into a quoted JS string inside an inline handler
        is one apostrophe away from arbitrary execution. Bind with data-* and
        addEventListener instead — .dataset returns the decoded value and never
        parses it as JS.
        """
        offenders = [
            m.group(0)
            for m in re.finditer(r'onclick="[^"]*"', _served_html())
            if "${" in m.group(0) and ("'" in m.group(0) or "\\`" in m.group(0))
        ]
        self.assertEqual(
            offenders,
            [],
            "inline handlers interpolate data into a quoted JS string:\n  "
            + "\n  ".join(offenders),
        )

    def test_gesc_escapes_both_quote_characters(self) -> None:
        """_gEsc must be safe in single- AND double-quoted attributes."""
        js = _served_js()
        gesc = _extract(js, "_gEsc")
        for ch, ent in [
            ("&", "&amp;"),
            ("<", "&lt;"),
            (">", "&gt;"),
            ('"', "&quot;"),
            ("'", "&#39;"),
        ]:
            self.assertIn(
                ent,
                gesc,
                f"_gEsc does not escape {ch!r} -> {ent}; it is unsafe wherever "
                f"that character can terminate the surrounding context",
            )

    def test_memory_fields_are_never_interpolated_raw(self) -> None:
        """A memory's ``source``/``id`` come from whichever AI client saved it.

        ``remember(source=...)`` stores the string unchanged, so a client could
        save ``<img src=x onerror=...>`` as its source and get script running in
        the owner's dashboard (this happened on the Conflicts screen). Every
        interpolation of these fields must go through _gEsc/esc/sourceLabel.
        """
        raw = re.findall(
            r"\$\{(?:it|item|i|d|m|r)\.(?:source|id|source_ref|cluster_id)\}", _served_js()
        )
        self.assertEqual(raw, [], f"memory fields interpolated without escaping: {raw}")

    def test_esc_is_not_used_inside_attributes(self) -> None:
        """esc() round-trips through textContent and escapes neither quote.

        It is fine for text nodes and a liability anywhere near an attribute.
        """
        html = _served_html()
        bad = re.findall(r'=\s*[\'"][^\'"]*\$\{esc\([^)]*\)\}', html)
        self.assertEqual(
            bad,
            [],
            f"esc() used inside an attribute value (use _gEsc): {bad}",
        )


class TestHostileUsbVolumeName(unittest.TestCase):
    """Plug in a maliciously-named drive and prove the dashboard stays inert."""

    HOSTILE = "/Volumes/x'); alert(1); //<img src=x onerror=alert(2)>"

    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        cls.js = _served_js()

    def test_hostile_volume_name_cannot_execute_or_break_the_handler(self) -> None:
        real = "\n\n".join(_extract(self.js, n) for n in ("api", "_gEsc", "icon", "usbRefresh"))
        program = (
            textwrap.dedent(f"""
            const HOSTILE = {json.dumps(self.HOSTILE)};
            const listeners = [];
            function makeNode(html) {{
              // Just enough DOM to observe what usbRefresh writes and binds.
              return {{
                innerHTML: '',
                querySelectorAll(sel) {{
                  const out = [];
                  const re = /data-idx="([^"]*)"\\s+data-path="([^"]*)"/g;
                  let m;
                  while ((m = re.exec(this.innerHTML))) {{
                    out.push({{
                      dataset: {{ idx: m[1], path: decodeEntities(m[2]) }},
                      addEventListener: (_e, fn) => listeners.push(fn),
                    }});
                  }}
                  return out;
                }},
              }};
            }}
            function decodeEntities(s) {{
              return s.replace(/&#39;/g, "'").replace(/&quot;/g, '"')
                      .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
                      .replace(/&amp;/g, '&');
            }}
            const el = makeNode();
            global.document = {{ getElementById: () => el }};
            global.fetch = async () => ({{
              ok: true, status: 200,
              json: async () => ({{ drives: [
                {{ path: HOSTILE, label: HOSTILE, has_vault: false, size_human: '8 GB' }}
              ] }}),
            }});
            let clickedPath = null;
            global.usbSelect = (i, p) => {{ clickedPath = p; }};

            (async () => {{
              await usbRefresh();
              const nodes = el.querySelectorAll('.usb-drive');
              listeners.forEach(fn => fn());
              console.log(JSON.stringify({{
                html: el.innerHTML,
                nodeCount: nodes.length,
                clickedPath,
              }}));
            }})();
            """)
            + "\n\n"
            + real
        )
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.mjs"
            f.write_text(program, encoding="utf-8")
            proc = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            self.fail(f"node harness failed:\n{proc.stderr}\n{proc.stdout}")
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        # 1. Nothing executable survives into the markup.
        #
        # NB: grepping for scary substrings ("onerror", "alert") is the WRONG test.
        # Correctly-escaped output legitimately *contains* those words as inert
        # display text inside &lt;img ... onerror=...&gt; — that is the fix working,
        # not a failure. What actually matters is structural: no character from the
        # payload may survive un-encoded in a position where it could terminate a
        # tag, an attribute, or a JS string.
        html = out["html"]
        self.assertNotIn(
            "<img",
            html,
            "a hostile volume name injected a real <img> tag into the dashboard",
        )
        for attr_val in re.findall(r'data-path="([^"]*)"', html):
            for ch in ("<", ">", '"', "'"):
                self.assertNotIn(
                    ch,
                    attr_val,
                    f"raw {ch!r} survived into a data-path attribute — it can "
                    f"terminate the attribute or a JS string built from it",
                )
        # The apostrophe breakout in its original form must be gone.
        self.assertNotIn(
            "');",
            html,
            "the quote-breakout sequence reached the DOM un-encoded",
        )

        # 2. The drive still works — escaping must not break the feature.
        self.assertEqual(out["nodeCount"], 1, "the hostile drive row was not rendered")
        self.assertEqual(
            out["clickedPath"],
            self.HOSTILE,
            "clicking the drive did not hand usbSelect() the exact original path — "
            "the value was mangled or the handler broke",
        )


if __name__ == "__main__":
    unittest.main()
