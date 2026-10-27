"""elesync — a local-first AI project workspace with shared MCP context.

One vault on your disk. Every AI reads from and writes to it over MCP.
"""

import os

from . import normalize
from .models import MEMORY_TYPES, MemoryItem
from .service import MemoryService
from .store import MemoryStore

__all__ = [
    "MemoryItem",
    "MEMORY_TYPES",
    "MemoryStore",
    "MemoryService",
    "normalize",
    "default_vault_dir",
]
__version__ = "1.24.79"

DEFAULT_VAULT = "~/EleSyncVault"


def default_vault_dir(vault_dir: str | None = None) -> str:
    """Resolve the vault directory: explicit arg → $ELESYNC_DIR → ~/EleSyncVault.

    This is the simple resolution used by modules that don't need the CLI's
    config-aware path (which also checks the persisted config.json). CLI
    commands use ``cli._resolve_vault()`` instead, which layers config on top.
    """
    return vault_dir or os.environ.get("ELESYNC_DIR", DEFAULT_VAULT)
