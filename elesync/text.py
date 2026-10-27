"""Shared text utilities.

Word tokenization used by conflict detection and the relationship graph.
Kept here so both modules use the same definition of "a word" rather than
each maintaining its own copy of the regex.
"""

from __future__ import annotations

import re

# A word is a run of lowercase letters, digits, and apostrophes.
# Apostrophes are included so "don't" stays one token, not two.
_WORD = re.compile(r"[a-z0-9']+")


def tokens(text: str | None) -> set[str]:
    """Return the set of word tokens in *text*, lowercased."""
    return set(_WORD.findall((text or "").lower()))
