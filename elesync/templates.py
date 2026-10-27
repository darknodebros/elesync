"""Memory templates — pre-defined structures for consistent capture.

The same thing gets recorded a dozen different ways over a vault's life: one
meeting note tagged `meeting`, the next tagged `meetings`, a third untagged and
phrased as a sentence. Recall degrades, and `find_conflicts` and
auto-supersession — both of which key on tags — quietly stop working on exactly
the memories that needed them most. A template fixes the shape once.

A template carries two halves, and both matter:

  * **Field defaults** — type, tags, scopes, source. This is the half that
    makes tag-driven machinery work, and it is the half a free-form note
    reliably gets wrong.
  * **A body skeleton** — ``"Met with {person} about {topic}."`` — so the
    content itself is comparable across captures.

Templates are plain JSON in ``<vault>/templates/``, one file per template, name
taken from the filename. Living in the vault (rather than in machine-local
config) means they travel with the directory and are carried in the
export/backup payload, so a restore on a new machine brings capture habits with
it rather than leaving them behind.

Rendering is deliberately **strict**: a missing placeholder is an error, and so
is an unexpected one. A template that silently rendered ``Met with {person}``
because a value was missing, or that silently dropped ``--set topci=x`` because
the key was misspelled, would produce exactly the inconsistent capture the
feature exists to prevent. Failing loudly at the moment of capture is cheap;
discovering it a year later in a recall that returns nothing is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import MEMORY_TYPES, normalize_tags

TEMPLATES_DIR_NAME = "templates"

# Placeholders are `{name}`; `{{` and `}}` are literal braces, matching str.format
# so the syntax is one people already know.
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z][A-Za-z0-9_]*)\}(?!\})")
_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

MAX_NAME_LENGTH = 48


class TemplateError(ValueError):
    """Raised for a malformed template or a bad render. Message is user-facing."""


@dataclass
class Template:
    name: str
    description: str = ""
    type: str = "note"
    tags: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    source: str = ""
    body: str = ""

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip().lower()
        if not _VALID_NAME.match(self.name):
            raise TemplateError(
                f"Invalid template name {self.name!r}: use lowercase letters, digits, "
                "'-' or '_', starting with a letter or digit."
            )
        if len(self.name) > MAX_NAME_LENGTH:
            raise TemplateError(f"Template name too long (max {MAX_NAME_LENGTH}): {self.name!r}")
        if self.type not in MEMORY_TYPES:
            raise TemplateError(
                f"Template {self.name!r} has unknown type {self.type!r}. "
                f"Valid types: {', '.join(sorted(MEMORY_TYPES))}."
            )
        self.tags = normalize_tags(self.tags)
        self.scopes = [s.strip() for s in (self.scopes or []) if str(s).strip()]

    @property
    def fields(self) -> list[str]:
        """Placeholder names in the body, in first-appearance order."""
        seen: list[str] = []
        for match in _PLACEHOLDER.finditer(self.body or ""):
            name = match.group(1)
            if name not in seen:
                seen.append(name)
        return seen

    def render(self, values: dict | None = None) -> str:
        """Fill the body from `values`.

        Raises TemplateError naming every missing and every unexpected field, in
        one message — fixing one typo per run is a bad way to spend an evening.
        """
        values = {str(k): v for k, v in (values or {}).items()}
        if not (self.body or "").strip():
            raise TemplateError(
                f"Template {self.name!r} has no body, so it cannot render content. "
                "Supply the content directly; the template still sets type/tags/scopes."
            )
        wanted = self.fields
        missing = [f for f in wanted if f not in values or str(values[f]).strip() == ""]
        unexpected = [k for k in values if k not in wanted]
        problems = []
        if missing:
            problems.append("missing: " + ", ".join(missing))
        if unexpected:
            problems.append("not in this template: " + ", ".join(sorted(unexpected)))
        if problems:
            raise TemplateError(
                f"Template {self.name!r} expects [{', '.join(wanted) or 'no fields'}] — "
                + "; ".join(problems)
            )

        def _sub(match: re.Match) -> str:
            return str(values[match.group(1)]).strip()

        out = _PLACEHOLDER.sub(_sub, self.body)
        # Unescape literal braces last, so a value containing `{x}` is never
        # re-scanned as a placeholder.
        return out.replace("{{", "{").replace("}}", "}").strip()

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "type": self.type,
            "tags": list(self.tags),
            "scopes": list(self.scopes),
            "source": self.source,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> Template:
        if not isinstance(data, dict):
            raise TemplateError(
                f"Template {name!r} must be a JSON object, got {type(data).__name__}."
            )
        unknown = set(data) - {"description", "type", "tags", "scopes", "source", "body", "name"}
        if unknown:
            # Silently ignoring a key means a typo'd "tag" (singular) looks like
            # it worked until recall comes up empty.
            raise TemplateError(
                f"Template {name!r} has unknown key(s): {', '.join(sorted(unknown))}. "
                "Valid keys: description, type, tags, scopes, source, body."
            )
        tags = data.get("tags") or []
        scopes = data.get("scopes") or []
        if isinstance(tags, str):
            tags = [tags]
        if isinstance(scopes, str):
            scopes = [scopes]
        return cls(
            name=data.get("name") or name,
            description=str(data.get("description") or ""),
            type=str(data.get("type") or "note"),
            tags=list(tags),
            scopes=list(scopes),
            source=str(data.get("source") or ""),
            body=str(data.get("body") or ""),
        )


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------
# Deliberately few. A long shipped list is a list nobody reads; these five cover
# the shapes that recur in an assistant-memory vault and that benefit most from
# consistent tagging. Any of them can be overridden by a same-named file in
# <vault>/templates/.

BUILTINS: dict[str, dict] = {
    "person": {
        "description": "Someone worth remembering, and how you know them.",
        "type": "relationship",
        "tags": ["person"],
        "body": "{name} — {role}. {detail}",
    },
    "meeting": {
        "description": "What was discussed and what was decided.",
        "type": "note",
        "tags": ["meeting"],
        "body": "Met with {who} on {date} about {topic}.\nDecided: {decided}.",
    },
    "decision": {
        "description": "A choice made, with the reasoning that justified it.",
        "type": "fact",
        "tags": ["decision"],
        "body": "Decided {decision} because {rationale}. Alternatives considered: {alternatives}.",
    },
    "preference": {
        "description": "How you like something done, so assistants stop re-asking.",
        "type": "preference",
        "tags": ["preference"],
        "body": "Prefers {preference} when {context}.",
    },
    "project": {
        "description": "An ongoing piece of work and its current state.",
        "type": "project",
        "tags": ["project"],
        "body": "{name}: {summary}. Status: {status}.",
    },
}


def builtins() -> dict[str, Template]:
    return {name: Template.from_dict(name, data) for name, data in BUILTINS.items()}


# ---------------------------------------------------------------------------
# Vault storage
# ---------------------------------------------------------------------------


def templates_dir(vault_dir) -> Path:
    return Path(vault_dir) / TEMPLATES_DIR_NAME


def load_all(vault_dir) -> tuple[dict[str, Template], dict[str, str]]:
    """Every available template, plus the files that could not be read.

    Returns ``(templates, errors)`` where `errors` maps filename → message. A
    single unreadable file must not take down `ele template list` — but it must
    not vanish either, so it is returned rather than logged and forgotten.

    A user template shadows a built-in of the same name; that is the documented
    way to customise one.
    """
    result = builtins()
    errors: dict[str, str] = {}
    directory = templates_dir(vault_dir)
    if not directory.is_dir():
        return result, errors
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tpl = Template.from_dict(path.stem, data)
        except (OSError, ValueError) as e:
            errors[path.name] = str(e)
            continue
        result[tpl.name] = tpl
    return result, errors


def get(vault_dir, name: str) -> Template:
    """One template by name, or TemplateError naming the close matches."""
    wanted = (name or "").strip().lower()
    available, errors = load_all(vault_dir)
    if wanted in available:
        return available[wanted]
    if wanted in errors:
        raise TemplateError(f"Template {wanted!r} could not be read: {errors[wanted]}")
    near = [n for n in sorted(available) if n.startswith(wanted[:3])] if wanted else []
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise TemplateError(
        f"No template named {wanted!r}. Available: {', '.join(sorted(available))}.{hint}"
    )


def save(vault_dir, template: Template) -> Path:
    """Write a template into the vault, overwriting a same-named user template."""
    directory = templates_dir(vault_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{template.name}.json"
    path.write_text(
        json.dumps(template.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def remove(vault_dir, name: str) -> bool:
    """Delete a user template. Returns False if there was no file to delete.

    A built-in cannot be deleted — only shadowed — so removing a user file that
    shadows one restores the built-in rather than leaving a hole.
    """
    path = templates_dir(vault_dir) / f"{(name or '').strip().lower()}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def user_defined(vault_dir) -> dict[str, Template]:
    """Only the templates backed by a file — what `export` needs to carry."""
    directory = templates_dir(vault_dir)
    out: dict[str, Template] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            out[path.stem] = Template.from_dict(
                path.stem, json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            continue  # surfaced by load_all; export must not fail on one bad file
    return out


def apply_defaults(template: Template, *, type=None, tags=None, scopes=None, source=None) -> dict:
    """Merge a template's field defaults with explicitly-supplied values.

    Explicit always wins — a template is a starting point, not a straitjacket.
    Tags are the exception: they **union** rather than replace, because the
    template's tag is the thing that makes the memory findable as a group, and
    adding one more tag at capture time should not silently drop it.
    """
    merged_tags = list(template.tags)
    for tag in normalize_tags(tags or []):
        if tag not in merged_tags:
            merged_tags.append(tag)
    return {
        "type": type or template.type,
        "tags": normalize_tags(merged_tags),
        "scopes": list(scopes) if scopes else list(template.scopes),
        "source": source or template.source or "manual",
    }
