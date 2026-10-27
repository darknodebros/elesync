"""Typed request objects shared by storage and transport layers."""

from __future__ import annotations

from dataclasses import dataclass

from . import scopes as scopes_mod


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Validated options for keyword, vector, or hybrid memory search."""

    text: str
    limit: int = 10
    source: str | None = None
    memory_type: str | None = None
    exclude_type: str | None = None
    grant: scopes_mod.Grant | None = None
    include_superseded: bool = False
    semantic_weight: float | None = None
    created_after: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("search limit must be at least 1")
        object.__setattr__(self, "limit", min(self.limit, 500))
        if self.semantic_weight is not None:
            object.__setattr__(
                self,
                "semantic_weight",
                max(0.0, min(1.0, float(self.semantic_weight))),
            )
