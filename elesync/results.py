"""Structured domain results shared by transports."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import MemoryItem


@dataclass(frozen=True, slots=True)
class RecallResult:
    """A recall operation without CLI/MCP presentation formatting."""

    memories: list[MemoryItem] = field(default_factory=list)
    conflicts: list[list[MemoryItem]] = field(default_factory=list)
    access_denied: bool = False
