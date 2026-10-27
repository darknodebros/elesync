"""Shared EleSync configuration access.

This module owns the config path and tolerant JSON loading/saving used by the
CLI and web layers. The compatibility wrappers in those layers remain during
the refactor so public behavior does not change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

Config = dict[str, Any]


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "EleSync" / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: Mapping[str, Any], path: Path | None = None) -> bool:
    """Persist configuration and report whether the write completed.

    Existing callers may continue to treat this as best-effort.  Callers that
    acknowledge an irreversible user action, such as first-run setup, can use
    the return value to avoid claiming success when Windows rejects the write.
    """
    import tempfile

    cfg = dict(config)
    path = path or config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — a crash mid-write must not leave `config.json`
        # truncated or half-written; readers should always see a fully-formed
        # prior state or the fully-formed new state.
        data = json.dumps(cfg, indent=2).encode("utf-8")
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
        )
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return False
    return True
