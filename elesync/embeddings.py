"""Optional semantic-embedding support.

The core stays dependency-free: everything here degrades gracefully when no
embedding model is installed. Semantic search needs fastembed (which pulls in
onnxruntime). Since onnxruntime doesn't have wheels for every platform, fastembed
is not a hard dependency — install the semantic extra explicitly to enable
`ele embed` or `--semantic` search. On platforms where onnxruntime can't install, semantic
search gracefully falls back to keyword search.

Design choices that keep the "no infrastructure" promise:
  * Vectors live as float32 blobs in the same SQLite index — no new datastore.
  * Similarity is brute-force cosine in pure Python — fast enough for a personal
    vault of thousands of memories, and nothing to run or tune.
  * The model is a small local ONNX model (no PyTorch), loaded only on demand.
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
import threading
import time
from array import array

_log = logging.getLogger("elesync.embeddings")

# Small, fast, local (fastembed uses ONNX runtime — no PyTorch). 384 dims.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def _try_install_fastembed() -> bool:
    """Attempt to pip-install fastembed. Returns True on success."""
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "fastembed>=0.7.0,<0.9.0",
                "pillow>=13.0.0",
                "--quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        _log.warning("auto-install of fastembed failed: %s", exc)
        return False


def pack(vec) -> bytes:
    """Serialise a float vector to a compact float32 blob."""
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> list[float]:
    """Inverse of `pack`."""
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a, b) -> float:
    """Cosine similarity of two equal-length vectors (pure stdlib).

    Raises ValueError if the vectors have different lengths, which would
    indicate a model mismatch (e.g. swapping embedding models on an existing
    vault) and produce silently-wrong scores if ignored.
    """
    if len(a) != len(b):
        raise ValueError(
            f"Embedding dimension mismatch: {len(a)} vs {len(b)}. "
            "Re-embed the vault with `ele embed` after changing the model."
        )
    dot = sa = sb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        sa += x * x
        sb += y * y
    if sa == 0.0 or sb == 0.0:
        return 0.0
    return dot / (math.sqrt(sa) * math.sqrt(sb))


class Embedder:
    """Wraps a fastembed model behind a tiny `str -> list[float]` interface.

    Any object exposing `embed_one(str) -> list[float]` works as an embedder, so
    the store can be driven by a stub in tests without pulling in the model."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from fastembed import TextEmbedding  # optional dep, imported lazily

        self.model_name = model_name
        self._model = TextEmbedding(model_name)
        # fastembed downloads/loads the model lazily on first embed. Force that
        # now with a tiny warm-up so any failure (offline, blocked download,
        # corrupt cache) raises here — where load_embedder() turns it into a
        # graceful keyword-search fallback instead of crashing mid-search.
        self.embed_one("warmup")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(list(texts))]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _download_with_spinner(model_name: str) -> Embedder | None:
    """Load the embedder with a spinner shown while fastembed downloads the model
    on first run. The download is blocking and silent without this."""
    spinner_done = threading.Event()
    spin_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _spin():
        i = 0
        while not spinner_done.is_set():
            message = f"\r   {spin_chars[i % len(spin_chars)]} Downloading {model_name} … "
            try:
                sys.stdout.write(message)
            except UnicodeEncodeError:
                # Legacy Windows consoles may still be cp1252. The animation
                # is cosmetic; use plain ASCII rather than crashing its thread.
                sys.stdout.write(f"\r   * Downloading {model_name}... ")
            sys.stdout.flush()
            time.sleep(0.1)
            i += 1
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        return Embedder(model_name)
    except Exception as exc:
        # Don't crash — callers fall back to keyword search. But log so a
        # firewall block, corrupt cache, or fastembed version mismatch isn't
        # invisible: the user ran `ele embed`, and got nothing; without
        # this they'd have no idea why.
        _log.warning(
            "semantic model %r failed to load: %s: %s", model_name, type(exc).__name__, exc
        )
        return None
    finally:
        spinner_done.set()
        t.join(timeout=1)


def load_embedder(model_name: str = DEFAULT_MODEL, auto_install: bool = False):
    """Return a ready-to-use `Embedder`, or None if semantic recall is unavailable.

    Returns None on any failure — fastembed not installable (onnxruntime has no
    wheel for this platform) or the model failing to load/download (offline,
    blocked, corrupt cache). Callers fall back to keyword search, so semantic
    mode never crashes the command; it just quietly degrades.

    Package installation is disabled by default. Install the semantic extra
    explicitly; library callers may deliberately opt in with auto_install=True.
    """
    try:
        import importlib

        importlib.import_module("fastembed")
    except ImportError:
        if not auto_install:
            _log.info("Semantic search unavailable; install the semantic extra explicitly")
            return None
        print("📦  Semantic search needs fastembed (one-time install) …")
        if not _try_install_fastembed():
            print(
                "⚠️   Could not install fastembed — semantic search will use keyword mode.\n"
                "     This is expected on platforms where onnxruntime has no wheels."
            )
            return None
        # Verify it actually imported (pip might succeed but import still fail)
        try:
            importlib.import_module("fastembed")
        except ImportError:
            print(
                "⚠️   fastembed installed but failed to import — semantic search will use keyword mode."
            )
            return None

    return _download_with_spinner(model_name)
