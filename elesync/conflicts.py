"""Provenance & conflict detection.

When two AIs (or two imports over time) record contradictory facts about you —
"Based in Westbrook" vs "Based in Portland" — EleSync shouldn't silently keep
both as if equally true. This module *surfaces* such cases, with each memory's
provenance (source + date + id), so you can decide and `forget` the wrong one.

Honest about its limits: real contradiction detection is a semantic (NLP/LLM)
problem. So this is a deliberately simple, deterministic heuristic — it flags
`fact` memories that look like they're about the *same thing* (high overlap)
but assert *different* values. It finds *candidates* to review, not ground
truth; expect the odd false positive (e.g. "visited Paris in 2020" vs "…2021"
can both be true). It never deletes anything — surfacing is the whole point.

Two similarity signals are supported, mirroring ``graph.py``'s degrade-gracefully
pattern:

  * **Semantic** (preferred when available): cosine similarity over the bge-small
    embeddings the vault already stores for semantic recall. Catches paraphrases
    ("lives in Westbrook" vs "resides in Westbrook") that word overlap misses.
  * **Lexical** (fallback): Jaccard word-set overlap. Used when ``ele embed``
    hasn't been run or a particular memory has no vector. No new dependency —
    pure stdlib.

The caller passes a ``vectors`` dict (``{id: list[float]}``) when embeddings are
available; ``is_conflict_edge`` uses cosine ≥ ``semantic_threshold`` for pairs
where both memories have vectors, and falls back to Jaccard ≥ ``threshold`` for
the rest. This means a partially-embedded vault still works — mixed-mode pairs
use the lexical signal, fully-embedded pairs use the semantic one.
"""

from __future__ import annotations

import hashlib
import threading
from array import array
from collections import OrderedDict

from .embeddings import cosine as _cosine
from .text import tokens as _tokens


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _similar(a, b, threshold: float) -> bool:
    """Jaccard overlap of word sets ≥ threshold — a cheap 'about the same thing' proxy.

    ``a`` and ``b`` are texts or their word sets; scans pass the sets from a
    ``_ScanFeatures`` so each memory is tokenised once rather than once per pair.
    """
    ta = a if isinstance(a, (set, frozenset)) else _tokens(a)
    tb = b if isinstance(b, (set, frozenset)) else _tokens(b)
    if not ta or not tb:
        return False
    la, lb = len(ta), len(tb)
    # The overlap can never beat the smaller set over the larger one, so a pair
    # whose sizes are that far apart is rejected without building a set. Exact:
    # rounding is monotonic, so the rounded bound caps the rounded Jaccard too.
    if min(la, lb) / max(la, lb) < threshold:
        return False
    shared = len(ta & tb)
    return shared / (la + lb - shared) >= threshold


class _ScanFeatures:
    """Each memory's normalised text and word set, derived once per scan.

    Pairwise scans used to re-derive both for every pair they compared: 663k
    tokenisations for an 815-fact vault, 97% of a 20-second dashboard load.
    Keyed by content, the only thing either depends on.
    """

    __slots__ = ("_norm_by_text", "_tokens_by_text")

    def __init__(self) -> None:
        self._norm_by_text: dict[str, str] = {}
        self._tokens_by_text: dict[str, set[str]] = {}

    def norm(self, item) -> str:
        found = self._norm_by_text.get(item.content)
        if found is None:
            found = self._norm_by_text[item.content] = _norm(item.content)
        return found

    def tokens(self, item) -> set[str]:
        found = self._tokens_by_text.get(item.content)
        if found is None:
            found = self._tokens_by_text[item.content] = _tokens(item.content)
        return found


def cluster_id(members) -> str:
    """Stable cluster ID: SHA-1 of sorted memory IDs (8 hex chars)."""
    key = ",".join(sorted(m.id for m in members))
    return hashlib.sha1(key.encode()).hexdigest()[:8]


# Surfacing conflicts for human review is cheap, so `find_conflicts` uses a
# permissive 0.5 bar. *Acting* on a conflict (auto-supersession) is expensive when
# wrong, so it demands a stricter match.
AUTO_SUPERSEDE_THRESHOLD = 0.6

# Cosine similarity floor for semantic mode — matches ``graph.py``'s
# ``SEMANTIC_FLOOR``. Over bge-small vectors, unrelated English text still
# scores ~0.6, so 0.72 is the bar that separates "about the same thing" from
# "both are English sentences". Used for the review surface (cheap surfacing).
SEMANTIC_THRESHOLD = 0.72

# Stricter bar for *acting* on a conflict (auto-supersession) in semantic mode —
# the lexical path raises 0.5 → 0.6 for the same reason: acting on a false match
# is costly, so the semantic bar is raised proportionally.
AUTO_SUPERSEDE_SEMANTIC_THRESHOLD = 0.82


def auto_supersede_candidate(
    new_item,
    live_facts,
    threshold: float = AUTO_SUPERSEDE_THRESHOLD,
    resolved_pairs: set | None = None,
    vectors: dict[str, list[float]] | None = None,
    semantic_threshold: float = AUTO_SUPERSEDE_SEMANTIC_THRESHOLD,
):
    """Return the single live fact that ``new_item`` cleanly supersedes, or None.

    Parameters
    ----------
    new_item:
        The just-saved memory that might be an update to an existing fact.
    live_facts:
        Candidate existing memories to check against (typically the vault's live,
        in-scope memories). Non-facts, superseded items, and ``new_item`` itself are
        filtered out internally.
    threshold:
        Jaccard similarity bar for the lexical fallback — stricter than the review
        default because acting (superseding) on a false match is costly.
    resolved_pairs:
        Pairs the user already resolved; forwarded to ``find_conflicts`` so a
        resolved conflict is never re-acted on.
    vectors:
        Optional ``{id: list[float]}`` embedding dict. When both memories in a
        pair have vectors, cosine ≥ ``semantic_threshold`` is used instead of
        Jaccard — catches paraphrase updates word overlap misses. ``None`` (the
        default) keeps the legacy lexical-only behaviour.
    semantic_threshold:
        Cosine floor for the semantic path. Defaults to the stricter
        ``AUTO_SUPERSEDE_SEMANTIC_THRESHOLD`` because acting on a false match is
        costly, mirroring the 0.5 → 0.6 raise the lexical path applies.

    Deliberately conservative — this runs on the write path, so it acts only on an
    unambiguous update. Two requirements together make "update" distinguishable
    from "two coexisting facts":

      * a **shared tag** — the new fact and the candidate must share at least one
        tag, the signal that they're about the same subject; an untagged save never
        qualifies; and
      * an **isolated pair** — using the same clustering as the review surface
        (``find_conflicts``), the new item must land in a cluster of *exactly two*.
        A run of mutually-similar facts collapses transitively into one *large*
        cluster, so no clean pair forms and nothing is superseded.

    Cross-scope pairs, already-resolved pairs, and non-facts all yield None.
    """
    if new_item.type != "fact":
        return None
    new_tags = set(new_item.tags)
    if not new_tags:
        # No subject marker to anchor an "update" claim — a shared tag is the
        # signal that two facts are about the same thing, so an untagged save
        # never auto-supersedes. (The "auto later" path can relax this once real
        # topic modelling exists.)
        return None
    # Confine clustering to same-scope live facts that share a tag with the new item
    # (+ the new item). Same-scope only, so an update never reaches across
    # compartments; live only, so history isn't a target; shared-tag only, so bulk
    # untagged near-duplicates can't collapse a real subject.
    scope = set(new_item.scopes)
    pool = [new_item]
    for f in live_facts:
        if f.id == new_item.id:
            continue
        if f.type != "fact" or f.superseded_by:
            continue
        if set(f.scopes) != scope:
            continue
        if not (new_tags & set(f.tags)):
            continue
        pool.append(f)

    clusters = find_conflicts(
        pool,
        threshold=threshold,
        resolved_pairs=resolved_pairs,
        vectors=vectors,
        semantic_threshold=semantic_threshold,
    )
    for cluster in clusters:
        ids = {m.id for m in cluster}
        if new_item.id in ids and len(cluster) == 2:
            other = next(m for m in cluster if m.id != new_item.id)
            return other
    return None


def is_conflict_edge(
    a,
    b,
    threshold: float,
    resolved: set,
    vectors: dict[str, list[float]] | None = None,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    features: _ScanFeatures | None = None,
) -> bool:
    """Whether two memories form a conflict edge — the single definition of that.

    Extracted so `find_conflicts` (whole-vault review) and `cluster_containing`
    (one item's component) can never drift apart on what counts as a conflict.
    Three exclusions come first, in order: an identical content_hash is a
    duplicate rather than a disagreement, content that normalises equal is the
    same fact recorded twice, and a pair the user already resolved must not
    resurface.

    When ``vectors`` is provided and *both* memories have an entry, the
    similarity test is cosine ≥ ``semantic_threshold`` (semantic mode). If
    either memory lacks a vector, or ``vectors`` is None, the test falls back
    to Jaccard word overlap ≥ ``threshold`` (lexical mode). This lets a
    partially-embedded vault use the best signal available for each pair.

    ``features`` lets a scan share one ``_ScanFeatures`` across all its pairs;
    without it, each text is derived for this pair alone.
    """
    feats = features if features is not None else _ScanFeatures()
    if a.content_hash == b.content_hash:
        return False
    if feats.norm(a) == feats.norm(b):
        return False
    if resolved and frozenset((a.id, b.id)) in resolved:
        return False
    if vectors is not None and a.id in vectors and b.id in vectors:
        try:
            return _cosine(vectors[a.id], vectors[b.id]) >= semantic_threshold
        except ValueError:
            # Dimension mismatch (model swap mid-vault). Fall back to lexical
            # rather than crash — `ele embed` fixes the underlying state.
            pass
    return _similar(feats.tokens(a), feats.tokens(b), threshold)


def cluster_containing(
    item,
    items,
    threshold: float = 0.5,
    types=("fact",),
    resolved_pairs: set | None = None,
    max_size: int | None = None,
    vectors: dict[str, list[float]] | None = None,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
):
    """The conflict cluster containing `item`, without clustering the whole vault.

    `find_conflicts` compares every eligible pair — O(n²) — which is the right
    shape for the review surface that genuinely wants every cluster. The write
    path does not: after saving one memory it only ever asks "what cluster did
    *this* land in?", and paying for the full quadratic scan on every save made
    `remember()` slow down as the vault grew.

    This walks outward from `item` instead, expanding across conflict edges. It
    returns the same connected component union-find would have produced — the
    edge relation is identical and reachability is the same question — but the
    cost is one pass per member found, and real components are one or two
    members, so in practice it is a single linear pass.

    ``vectors`` and ``semantic_threshold`` are forwarded to ``is_conflict_edge``
    so the semantic signal is used when available (see that function's docstring).

    `max_size` stops the walk once the component exceeds that many members. Both
    callers only care whether the component is an isolated *pair*, and a
    long similarity chain is exactly the case where continuing to expand costs
    the most and tells them the least.

    Returns the component (including `item`) when it has ≥2 members, else [].
    A returned list longer than `max_size` means "at least this big" — the walk
    stopped early and the true component may be larger.
    """
    if item.type not in types:
        return []
    resolved = resolved_pairs or set()
    pool = [it for it in items if it.type in types and it.id != item.id]
    features = _ScanFeatures()
    component = {item.id: item}
    frontier = [item]
    while frontier:
        current = frontier.pop()
        for other in pool:
            if other.id in component:
                continue
            if is_conflict_edge(
                current, other, threshold, resolved, vectors, semantic_threshold, features
            ):
                component[other.id] = other
                frontier.append(other)
                if max_size is not None and len(component) > max_size:
                    return list(component.values())
    return list(component.values()) if len(component) >= 2 else []


# Smaller scans (recall hits, the auto-supersede pool) are cheap to redo and would
# only push the expensive whole-vault result out of the cache.
_CACHE_MIN_ITEMS = 50
_SCAN_CACHE_SIZE = 8
_scan_cache: OrderedDict[bytes, list[list[int]]] = OrderedDict()
_scan_cache_lock = threading.Lock()


def find_conflicts(
    items,
    threshold: float = 0.5,
    types=("fact",),
    resolved_pairs: set | None = None,
    vectors: dict[str, list[float]] | None = None,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
) -> list[list]:
    """Group memories that look like they concern the same thing but disagree.

    Parameters
    ----------
    items:
        The memories to scan. Each needs ``.type``, ``.content``, ``.content_hash``
        and ``.id``. Items whose ``type`` is not in ``types`` are ignored entirely
        (not just excluded from clusters), so passing a mixed list is fine.
    threshold:
        Minimum Jaccard word-overlap (0..1) for two memories to count as "about the
        same thing" in the lexical fallback. Higher = stricter (fewer, tighter
        clusters). Callers use a permissive 0.5 for review surfacing and a stricter
        ``AUTO_SUPERSEDE_THRESHOLD`` for the act-on-it path.
    types:
        Which memory types are eligible (default ``("fact",)``). Only facts carry
        singular, contradictable values; preferences/projects/relationships
        routinely coexist without conflicting, so they're excluded by default.
    resolved_pairs:
        Optional set of ``frozenset({id_a, id_b})`` pairs already resolved by the
        user. A matching pair is treated as non-conflicting, so a resolved conflict
        never resurfaces.
    vectors:
        Optional ``{id: list[float]}`` embedding dict. When both memories in a
        pair have vectors, cosine ≥ ``semantic_threshold`` is used instead of
        Jaccard — catches paraphrase conflicts ("lives in Westbrook" vs "resides
        in Westbrook") that word overlap misses. ``None`` keeps the legacy
        lexical-only behaviour; an empty dict is equivalent.
    semantic_threshold:
        Cosine floor for the semantic path (default ``SEMANTIC_THRESHOLD``).
        Callers that *act* on conflicts pass a stricter value
        (``AUTO_SUPERSEDE_SEMANTIC_THRESHOLD``).

    Returns
    -------
    A list of clusters (each a list of ≥2 memories). Order follows first appearance
    in ``items``. A memory appears in at most one cluster.

    Algorithm
    ---------
    Two passes:

    1. **Pairwise compare** every eligible pair (O(n²) in the number of eligible
       items — fine for a personal vault's fact count). A pair is a conflict edge
       when the two are *similar* (semantic cosine ≥ ``semantic_threshold`` when
       both have vectors, else Jaccard ≥ ``threshold``) yet *not the same fact*.
       Three cases are explicitly excluded first: an identical ``content_hash``
       (a duplicate, not a disagreement), content that normalises equal (the same
       fact re-recorded by another source — agreement), and any pair in
       ``resolved_pairs``.

    2. **Connected components** via union-find (disjoint-set). Each conflict edge
       ``union``s its two endpoints; ``find`` returns a component's representative,
       with path compression (``parent[x] = parent[parent[x]]``) to keep repeated
       lookups near-flat. Components of size ≥2 are the returned clusters.

    Union-find is what makes conflicts *transitive*: if A conflicts with B and B
    with C, all three land in one cluster even when A and C aren't directly similar.
    That's deliberate — a drifting fact ("Portland" → "Seattle" → "Boston") should
    surface as one review group, not three disjoint pairs. The same property is
    what ``auto_supersede_candidate`` leans on when it insists on an *isolated
    pair*: a longer similarity chain collapses into one big cluster, so no clean
    two-member "this updates that" pair exists and nothing is auto-superseded.

    Caching
    -------
    The dashboard runs the whole-vault scan on every Home load, and the answer
    only changes when the vault does. Large scans are remembered under a digest
    of everything that decides an edge (each memory's id, hash and text, the
    resolved pairs, the vectors and thresholds), so an unchanged vault answers
    at once while any edit, resolution or re-embed, from this process or
    another, is a new digest and recomputes."""
    resolved = resolved_pairs or set()
    eligible = [it for it in items if it.type in types]
    key = None
    groups: list[list[int]] | None = None
    if len(eligible) >= _CACHE_MIN_ITEMS:
        key = _scan_key(eligible, threshold, resolved, vectors, semantic_threshold)
        with _scan_cache_lock:
            groups = _scan_cache.get(key)
            if groups is not None:
                _scan_cache.move_to_end(key)
    if groups is None:
        groups = _cluster_positions(eligible, threshold, resolved, vectors, semantic_threshold)
        if key is not None:
            with _scan_cache_lock:
                _scan_cache[key] = groups
                while len(_scan_cache) > _SCAN_CACHE_SIZE:
                    _scan_cache.popitem(last=False)
    return [[eligible[k] for k in group] for group in groups]


def _scan_key(eligible, threshold, resolved, vectors, semantic_threshold) -> bytes:
    """Digest of every input that can change which pairs are conflict edges."""
    digest = hashlib.blake2b(digest_size=16)

    def feed(data: bytes) -> None:
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)

    feed(repr((threshold, semantic_threshold, len(eligible))).encode())
    for item in eligible:
        feed(item.id.encode())
        feed(item.content_hash.encode())
        # The stored hash survives hand-edits (reindex keeps it), so the text
        # itself is part of the key.
        feed(item.content.encode("utf-8", "surrogatepass"))
    pairs = sorted(tuple(sorted(pair)) for pair in resolved)
    feed(repr(len(pairs)).encode())
    for pair in pairs:
        feed("\0".join(pair).encode())
    if vectors:
        for item in eligible:
            vector = vectors.get(item.id)
            if vector is not None:
                feed(item.id.encode())
                feed(array("d", vector).tobytes())
    return digest.digest()


def _cluster_positions(
    eligible, threshold, resolved, vectors, semantic_threshold
) -> list[list[int]]:
    """The union-find pass of ``find_conflicts``, as positions into ``eligible``."""
    parent = list(range(len(eligible)))  # each item starts in its own component
    features = _ScanFeatures()

    def find(x):
        # Representative of x's component, compressing the path on the way up so
        # later finds are near-constant time.
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        # Merge the two components (no-op if already merged).
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for p in range(len(eligible)):
        for q in range(p + 1, len(eligible)):
            if is_conflict_edge(
                eligible[p],
                eligible[q],
                threshold,
                resolved,
                vectors,
                semantic_threshold,
                features,
            ):
                union(p, q)  # record a conflict edge

    # Bucket positions by their component representative; keep only real
    # clusters (≥2), in input order of each cluster's first member.
    groups: dict[int, list[int]] = {}
    for k in range(len(eligible)):
        groups.setdefault(find(k), []).append(k)
    return [g for g in groups.values() if len(g) >= 2]


def all_pairs(members) -> list[tuple[str, str]]:
    """Return all (id_a, id_b) pairs in canonical (sorted) order for a cluster."""
    ids = sorted(m.id for m in members)
    pairs = []
    for p in range(len(ids)):
        for q in range(p + 1, len(ids)):
            pairs.append((ids[p], ids[q]))
    return pairs
