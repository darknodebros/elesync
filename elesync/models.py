"""Normalized memory schema.

Every source (ChatGPT, Claude, Gemini, manual notes, ...) is mapped into a single
`MemoryItem` shape. This is the contract the whole system depends on: adapters
produce MemoryItems, the store persists MemoryItems, and the MCP server serves
MemoryItems back to any AI client.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import scopes as scopes_mod

# Coarse taxonomy. Kept deliberately small so it stays useful across providers.
MEMORY_TYPES = {
    "fact",  # "User's name is Alex", "Lives in Westbrook"
    "preference",  # "Prefers direct, no-fluff answers"
    "project",  # "Helios side project", "open-source CLI tool"
    "relationship",  # "Has a younger sister"
    "conversation",  # a captured exchange, summarised or raw
    "note",  # free-form, manually added
}

VALID_SOURCES_HINT = (
    "chatgpt",
    "claude",
    "gemini",
    "grok",
    "deepseek",
    "perplexity",
    "copilot",
    "whatsapp",
    "manual",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_for_hash(text: str) -> str:
    """Lower-case + collapse whitespace so trivially-different copies of the same
    memory (e.g. re-exported across sessions) hash identically and dedupe."""
    return re.sub(r"\s+", " ", text.strip().lower())


# Tag hygiene. Tags are free-form labels, but unbounded/garbage input has to be
# tamed before it reaches the index and the markdown frontmatter: an over-long
# tag bloats every note it touches, control characters corrupt the frontmatter
# line, and an unbounded count lets one memory carry a whole document's worth of
# labels. These bound the damage without rejecting a legitimate tag.
MAX_TAG_LENGTH = 64  # characters; longer tags are truncated, not dropped
MAX_TAGS = 32  # per memory; extra tags beyond this are dropped
_TAG_SPLIT = re.compile(r"[,;]")
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_tags(raw) -> list[str]:
    """Turn free-form tag input into a clean, bounded list of labels.

    Accepts a list or a "a, b; c" string transparently. Each tag is stripped of
    surrounding and control whitespace, internal runs of whitespace are collapsed,
    control characters are removed, and the result is truncated to
    ``MAX_TAG_LENGTH``. Empty results are dropped, duplicates are removed
    (first spelling wins, order preserved), and the list is capped at ``MAX_TAGS``.
    Idempotent, so re-normalising an already-clean list is a no-op."""
    if raw is None:
        return []
    parts = raw if isinstance(raw, list) else _TAG_SPLIT.split(str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = _CTRL_CHARS.sub("", str(part))
        tag = re.sub(r"\s+", " ", tag).strip()
        if len(tag) > MAX_TAG_LENGTH:
            tag = tag[:MAX_TAG_LENGTH].strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


# Direct links between memories. A link is "<relation>:<target id>" and lives on
# its source memory. Supersession ("replaces") is deliberately not a relation: it
# already has its own lifecycle (hidden from recall, walkable with `history`).
LINK_RELATIONS = ("related", "caused_by", "depends_on", "supports", "contradicts", "part_of")
MAX_LINKS = 64  # per memory; keeps one note's front-matter line bounded
_LINK_TARGET = re.compile(r"[A-Za-z0-9_-]{1,64}")


def normalize_links(raw) -> list[str]:
    """Turn link input into a clean, bounded list of ``relation:target`` strings.

    Accepts a list or a "a:x, b:y" string. Unknown relations and malformed
    targets are dropped rather than rejected, so a hand-edited note never
    fails to load. Duplicates are removed (order kept) and the list is capped
    at ``MAX_LINKS``. Idempotent."""
    if raw is None:
        return []
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[str] = []
    for part in parts:
        relation, sep, target = str(part).strip().partition(":")
        relation, target = relation.strip().lower(), target.strip()
        if not sep or relation not in LINK_RELATIONS or not _LINK_TARGET.fullmatch(target):
            continue
        link = f"{relation}:{target}"
        if link in out:
            continue
        out.append(link)
        if len(out) >= MAX_LINKS:
            break
    return out


def split_link(link: str) -> tuple[str, str]:
    """``"caused_by:abc"`` -> ``("caused_by", "abc")`` for a normalized link."""
    relation, _, target = link.partition(":")
    return relation, target


@dataclass
class MemoryItem:
    content: str
    source: str = "manual"
    type: str = "note"
    tags: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)  # [] == "general" (visible to all)
    confidence: float = 1.0
    created_at: str = field(default_factory=_now)
    source_ref: str = ""  # provenance: original conversation id / export filename
    id: str = ""  # stable uuid (assigned on creation if blank)
    content_hash: str = ""  # for dedup / near-dup detection
    superseded_by: str = ""  # id of the memory that replaced this one ("" == live)
    superseded_at: str = ""  # ISO-8601 UTC timestamp of when it was superseded
    links: list[str] = field(default_factory=list)  # "relation:target_id" (see LINK_RELATIONS)

    def __post_init__(self) -> None:
        # One choke point for tag hygiene: every construction path (CLI, service,
        # web, adapters, from_dict) normalises and bounds tags identically here.
        self.tags = normalize_tags(self.tags)
        # Scopes are normalised through one shared parser: lower-cased, de-duped,
        # with the implicit "general" label dropped so [] is the only spelling of
        # "visible to everyone". Accepts a list or a "a, b" string transparently.
        self.scopes = scopes_mod.parse(self.scopes)
        if self.type not in MEMORY_TYPES:
            self.type = "note"
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not self.content_hash:
            self.content_hash = self.compute_hash()
        if not self.id:
            self.id = uuid.uuid4().hex
        self.links = [
            link for link in normalize_links(self.links) if split_link(link)[1] != self.id
        ]

    def compute_hash(self) -> str:
        basis = f"{self.source}|{self.type}|{_normalize_for_hash(self.content)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MemoryItem:
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)  # type: ignore[arg-type]

    @classmethod
    def from_markdown(cls, text: str) -> MemoryItem:
        """Inverse of `to_markdown`: parse a note's YAML front-matter + body back
        into a MemoryItem. Tolerant of hand-edits (extra blank lines, missing
        optional keys); preserves the stored id and content_hash so a reindex is a
        faithful rebuild rather than a reimport. Raises ValueError if there's no
        front-matter block to parse."""
        if not text.startswith("---"):
            raise ValueError("note has no front-matter block")
        _, fm, body = text.split("---", 2)
        meta: dict = {}
        for line in fm.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if not key:
                continue
            if key == "tags":
                meta[key] = [t.strip() for t in val.strip("[]").split(",") if t.strip()]
            elif key == "scopes":
                meta[key] = [s.strip() for s in val.strip("[]").split(",") if s.strip()]
            elif key == "links":
                meta[key] = val.strip("[]")
            else:
                meta[key] = val
        meta["content"] = body.lstrip("\n").rstrip("\n")
        return cls.from_dict(meta)

    def to_markdown(self) -> str:
        """Human-readable, Obsidian-compatible: YAML front-matter + body.
        This is what makes the vault *yours* — readable in any text editor."""
        tags = ", ".join(self.tags)
        scopes = ", ".join(self.scopes)
        head = (
            "---\n"
            f"id: {self.id}\n"
            f"source: {self.source}\n"
            f"type: {self.type}\n"
            f"tags: [{tags}]\n"
            f"scopes: [{scopes}]\n"
            f"confidence: {self.confidence}\n"
            f"created_at: {self.created_at}\n"
            f"source_ref: {self.source_ref}\n"
            f"content_hash: {self.content_hash}\n"
        )
        # Only superseded notes carry these lines, so a live note's front-matter
        # is unchanged from earlier versions (no vault-wide churn on upgrade).
        if self.superseded_by:
            head += f"superseded_by: {self.superseded_by}\n"
            head += f"superseded_at: {self.superseded_at}\n"
        # Same rule for links: only linked notes carry the line.
        if self.links:
            head += f"links: [{', '.join(self.links)}]\n"
        return head + "---\n\n" + f"{self.content}\n"
