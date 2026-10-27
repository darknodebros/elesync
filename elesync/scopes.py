"""Scoped sharing — per-client memory compartments.

One vault, but not every AI should see every memory. Your writing style is fine
for any assistant to read; your legal correspondence or health notes are not.
Scopes are how EleSync lets a single vault present a *different slice of itself*
to each client — the everyday-user version of what Anuma gates by category.

The whole model in four sentences:

  * A memory carries a set of **scope labels**. No labels means ``general`` —
    visible to every client (so every memory that already exists keeps behaving
    exactly as before; scoping is opt-in *restriction*, never a new requirement).
  * A client carries a **grant**: the set of scopes it's allowed to see. The grant
    is just an env var (``ELESYNC_SCOPES``) on that client's MCP config — the same
    local-first, no-server pattern as ``ELESYNC_DIR`` and ``ELESYNC_PASSPHRASE``.
  * An *unset* grant (``None``) is **unrestricted**: the full vault. That's the
    default — what the owner's own CLI uses, and what a freshly-onboarded client
    gets until you deliberately scope it down.
  * A *set* grant sees ``general`` memories plus any memory whose labels intersect
    the grant. So ``ELESYNC_SCOPES=general,writing`` sees general + writing and
    nothing tagged ``legal``.

Everything here is pure, deterministic, and dependency-free. Visibility is decided
in exactly one function (``is_visible``) so the rule the store, the service and the
MCP server all enforce is the same rule — and it's unit-tested directly.

**Be clear-eyed about the boundary.** Scopes are *organisational hygiene*, not a
cryptographic wall. The boundary is the per-client config *you* control: a client
you point at the vault with no ``ELESYNC_SCOPES`` set sees everything, by design.
If you need data unreadable even to a process that opens the vault, that's what
encryption at rest is for — the two compose (an encrypted vault still scopes).
"""

from __future__ import annotations

from collections.abc import Iterable

# The implicit "everyone can see it" bucket. A memory with no scopes is in
# `general`; writing this label explicitly is allowed but normalises back to the
# empty set, so there is exactly one on-disk representation of "unrestricted".
GENERAL = "general"

# A grant is either None (unrestricted — the full vault) or a frozenset of the
# *restricted* scope labels a client may see (general is always implied).
Grant = frozenset  # type alias for readability at call sites


def parse(raw: object) -> list[str]:
    """Normalise a scopes value into a clean, ordered, de-duplicated label list.

    Accepts a list (from a dataclass / JSON) or a string (``"legal, Work"`` from a
    CLI flag or YAML front-matter). Labels are lower-cased and trimmed; blanks and
    the implicit ``general`` label are dropped (general == no scopes); order of
    first appearance is preserved so round-trips are stable and diffs are quiet.

        parse("Legal, work ,, general, legal") -> ["legal", "work"]
    """
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, str):
        items = raw.replace(";", ",").split(",")
    elif isinstance(raw, Iterable):
        items = [str(x) for x in raw]
    else:
        items = [str(raw)]
    seen: set[str] = set()
    out: list[str] = []
    for label in items:
        label = label.strip().lower()
        if not label or label == GENERAL or label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def grant_from(raw: object) -> Grant | None:
    """Build a client's grant from an env value / CLI flag.

    Returns ``None`` (unrestricted — the full vault) when nothing is configured,
    so the default behaviour is identical to a world without scopes. When the
    value *is* present, the client becomes scoped: it sees ``general`` plus the
    labels listed. ``ELESYNC_SCOPES=general`` therefore means "general only"
    (a frozenset with no restricted labels), which is a meaningfully different,
    tighter grant than leaving it unset.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        # Present-but-empty (e.g. `ELESYNC_SCOPES=`) reads as "no extra grant" —
        # general only — not as unrestricted. Unset is the way to say unrestricted.
        return frozenset()
    return frozenset(parse(raw))


def is_visible(item_scopes: object, grant: Grant | None) -> bool:
    """The one visibility rule the whole system shares.

    * A ``general`` memory (no scope labels) is visible to everyone.
    * An unrestricted client (``grant is None``) sees every memory.
    * Otherwise a restricted memory is visible only if its labels intersect the
      client's grant.
    """
    restricted = frozenset(parse(item_scopes))
    if not restricted:  # general — everyone sees it
        return True
    if grant is None:  # unrestricted client — sees everything
        return True
    return bool(restricted & grant)


def is_readonly(raw: object) -> bool:
    """Check whether a raw scopes value requests read-only access.

    The convention: if the env value starts with ``readonly:`` (e.g.
    ``ELESYNC_SCOPES=readonly:writing,legal``) the client is scoped **and**
    write-locked.  Plain ``readonly`` (no colon, no labels) means
    "read-only, general only".  Anything else is read-write.
    """
    if not isinstance(raw, str):
        return False
    return raw.strip().lower().startswith("readonly")


def strip_readonly(raw: str) -> str:
    """Remove the ``readonly:`` prefix so ``grant_from`` can parse the labels."""
    s = raw.strip()
    if s.lower() == "readonly":
        return ""  # general only, read-only
    if s.lower().startswith("readonly:"):
        return s[len("readonly:") :]
    return s


def describe(grant: Grant | None, *, readonly: bool = False) -> str:
    """Human-readable one-liner for a grant — used in CLI/doctor/MCP output."""
    suffix = " (read-only)" if readonly else ""
    if grant is None:
        return f"unrestricted (full vault){suffix}"
    if not grant:
        return f"general only{suffix}"
    return "general + " + ", ".join(sorted(grant)) + suffix


def label_of(item_scopes: object) -> str:
    """Render a memory's scopes for display: ``general`` or ``{a, b}``."""
    labels = parse(item_scopes)
    return GENERAL if not labels else "{" + ", ".join(labels) + "}"
