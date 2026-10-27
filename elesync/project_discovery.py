"""Suggest projects from memories that are not yet in any project.

Imported AI history is usually a pile of conversations about a handful of real
efforts — a trip, a launch, a job search. This groups unfiled memories around
the distinctive words they share and proposes a named project per group, so a
new user can go from "I imported my ChatGPT export" to organised projects in
one step.

Deliberately simple and deterministic (no model, no new dependency): a greedy
cover over distinctive terms. A term anchors a group when it appears in several
memories but is not common across the whole history; each memory joins at most
one group. Names come from the most frequent two-word phrase containing the
anchor ("kyoto trip"), falling back to the anchor alone. These are suggestions
for the owner to review and rename, never applied automatically.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_WORD = re.compile(r"[a-z][a-z'-]+")

_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are aren't as at be because been
    before being below between both but by can can't cannot could couldn't did didn't do does
    doesn't doing don't down during each few for from further had hadn't has hasn't have haven't
    having he her here hers herself him himself his how i if in into is isn't it it's its itself
    just let let's me more most much must my myself no nor not now of off on once only or other
    our ours ourselves out over own same she should shouldn't so some such than that that's the
    their theirs them themselves then there there's these they they're this those through to too
    under until up upon very was wasn't we we're were weren't what what's when where which while
    who whom why will with won't would wouldn't you you're your yours yourself yourselves
    user assistant human model system chatgpt claude gemini grok copilot perplexity deepseek ai
    conversation chat message messages please help thanks thank want need needs like make makes
    made get gets got know think could would should one two also use using used new good great
    sure yes okay ok here there something anything everything nothing thing things lot bit way
    time times day days today tomorrow yesterday week weeks year years month months really
    maybe still even well back going go goes went see look looks looking give given take takes
    say says said tell told ask asked answer question questions idea ideas start started first
    last next best better right left also etc via per using based around within without across
    memory memories note notes project projects md json http https www com html
    january february march april may june july august september october november december
    monday tuesday wednesday thursday friday saturday sunday
    """.split()
)

TITLE_WEIGHT = 3
MAX_ANCHOR_SHARE = 0.5


def _title_of(content: str) -> str:
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:90]
    return ""


def _words(text: str) -> list[str]:
    return [
        w.strip("'-")
        for w in _WORD.findall((text or "").lower())
        if len(w.strip("'-")) >= 3 and w.strip("'-") not in _STOPWORDS
    ]


def _bigrams(words: list[str]) -> list[str]:
    return [f"{a} {b}" for a, b in zip(words, words[1:], strict=False) if a != b]


def _display_name(phrase: str) -> str:
    return phrase[:1].upper() + phrase[1:]


def suggest_projects(
    items,
    *,
    existing_names=(),
    max_projects: int = 5,
    min_size: int = 2,
) -> list[dict]:
    """Group ``items`` (MemoryItem-like: id, content, source, type) into up to
    ``max_projects`` suggested projects of at least ``min_size`` memories."""
    docs = []
    for item in items:
        title = _title_of(item.content)
        body = (item.content or "")[:4000]
        words = _words(body)
        title_words = _words(title)
        weighted = Counter(words)
        for w in title_words:
            weighted[w] += TITLE_WEIGHT
        docs.append(
            {
                "item": item,
                "title": title,
                "terms": set(weighted),
                "weights": weighted,
                "bigrams": set(_bigrams(words) + _bigrams(title_words)),
                "title_bigrams": set(_bigrams(title_words)),
                "title_terms": set(title_words),
            }
        )
    total = len(docs)
    if total < min_size:
        return []

    postings: dict[str, set[int]] = {}
    for index, doc in enumerate(docs):
        for term in doc["terms"]:
            postings.setdefault(term, set()).add(index)
    df = Counter({term: len(ids) for term, ids in postings.items()})
    title_df = Counter(term for doc in docs for term in doc["title_terms"])
    ceiling = max(min_size, int(total * MAX_ANCHOR_SHARE))
    existing = {n.strip().lower() for n in existing_names if n and n.strip()}
    existing_words = {w for name in existing for w in _words(name)}

    unassigned = set(range(total))
    suggestions: list[dict] = []
    while len(suggestions) < max_projects:
        best = None
        for term, freq in df.items():
            if freq < min_size or term in existing_words:
                continue
            # Too common to be a topic — unless it names every memory it is in
            # (e.g. what is left after other projects were filed is all "Helios").
            if freq > ceiling and title_df[term] < freq:
                continue
            members = sorted(postings[term] & unassigned)
            if len(members) < min_size:
                continue
            idf = math.log(1 + total / freq)
            strength = sum(min(docs[i]["weights"][term], 6) for i in members)
            score = len(members) * idf + 0.1 * strength
            if best is None or score > best[0] or (score == best[0] and term < best[1]):
                best = (score, term, members)
        if best is None:
            break
        _, anchor, members = best
        unassigned -= set(members)
        df[anchor] = 0  # an anchor never anchors twice

        group_df = Counter(t for i in members for t in docs[i]["terms"] if t != anchor)
        co_terms = [
            t
            for t, c in sorted(
                group_df.items(),
                key=lambda kv: (-(kv[1] / max(1, df.get(kv[0], 1))), -kv[1], kv[0]),
            )
            if c >= max(2, math.ceil(len(members) / 2))
        ][:4]
        # Name: the two-word phrase with the anchor used by the most memories;
        # ties go to phrases that appear in conversation titles, then A–Z.
        phrases: Counter[str] = Counter()
        in_titles: Counter[str] = Counter()
        for i in members:
            for phrase in docs[i]["bigrams"]:
                if anchor in phrase.split():
                    phrases[phrase] += 1
                    in_titles[phrase] += phrase in docs[i]["title_bigrams"]
        common = sorted(
            (p for p, c in phrases.items() if c >= 2),
            key=lambda p: (-phrases[p], -in_titles[p], p),
        )
        name = _display_name(common[0] if common else anchor)
        if name.lower() in existing:
            continue

        ordered = sorted(members, key=lambda i: (docs[i]["title"], docs[i]["item"].id))
        member_items = sorted((docs[i]["item"] for i in members), key=lambda it: it.id)
        titles = [docs[i]["title"] for i in ordered if docs[i]["title"]]
        suggestions.append(
            {
                "name": name,
                "anchor": anchor,
                "terms": co_terms,
                "memory_ids": [it.id for it in member_items],
                "count": len(member_items),
                "sources": sorted({it.source for it in member_items if it.source}),
                "samples": titles[:3],
            }
        )
    return suggestions
