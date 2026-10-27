"""Shared application context and dependency construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, load_config


@dataclass(slots=True)
class ApplicationContext:
    """Runtime dependencies shared by CLI, web, and MCP entry points."""

    vault_dir: Path
    config: Config
    store: Any
    service: Any
    auth: Any = None
    audit: Any = None
    embeddings: Any = None


def create_context(
    vault_dir: str | Path,
    *,
    config: Config | None = None,
    store: Any = None,
    service: Any = None,
    grant=None,
    write_scopes=None,
    readonly: bool = False,
    audit: Any = None,
    embedder: Any = None,
    key: bytes | None = None,
    project_id: str | None = None,
    project_read: bool = True,
    project_write: bool = True,
) -> ApplicationContext:
    """Create one application context while allowing entry-point injection.

    ``store`` and ``service`` can be supplied by callers that already performed
    encrypted-vault or transport-specific setup. This keeps construction
    centralized without changing those entry points' security behavior.
    """
    from .service import MemoryService
    from .store import MemoryStore

    root = Path(vault_dir).expanduser()
    cfg = config if config is not None else load_config()
    active_store = store or MemoryStore(root, embedder=embedder, key=key)
    active_service = service or MemoryService(
        active_store,
        grant=grant,
        write_scopes=write_scopes,
        readonly=readonly,
        audit=audit,
        project_id=project_id,
        project_read=project_read,
        project_write=project_write,
    )
    return ApplicationContext(
        vault_dir=root,
        config=cfg,
        store=active_store,
        service=active_service,
        audit=audit,
        embeddings=embedder,
    )
