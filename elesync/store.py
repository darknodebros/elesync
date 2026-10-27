"""Local-first storage.

Two representations, both on the user's own disk, no server involved:

  1. Markdown files (notes/<id>.md) — the source of truth, human-readable,
     git-able, Obsidian-compatible. This is the "you own it" guarantee.
  2. SQLite index (index.db, FTS5 when available) — fast full-text search and
     the query surface the MCP server sits on top of.

Dedup is by content_hash so re-importing the same export is idempotent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from . import crypto, embeddings
from . import scopes as scopes_mod
from .models import MemoryItem, normalize_links
from .queries import SearchQuery
from .store_links import LinkMixin
from .store_projects import ProjectMixin
from .store_projects import document_lifecycle as _document_lifecycle

VAULT_SCHEMA_VERSION = 1

_log = logging.getLogger("elesync.store")

META_FILE = "meta.json"
PROJECTS_FILE = "projects.json"
PROJECTS_ENCRYPTED_FILE = "projects.json.enc"
PROJECTS_FORMAT_VERSION = 2

# When a search post-filters in Python (by source/type/scope, or hiding
# superseded), the FTS/vector layer must over-fetch candidates so enough survive
# the filter to fill `limit`. A *fixed* factor can't win for every filter: too
# small and a selective filter (a narrow scope grant, a rare source) under-returns
# even though more matching rows exist deeper in the ranking; too large and every
# permissive query pays for rows it never needs. So the factor below is only the
# *starting* width — `search` widens adaptively (doubling) when a fetch comes back
# saturated yet still yields < limit after filtering, and stops as soon as it has
# enough or the underlying search is exhausted. MAX_FILTER_OVERFETCH caps the worst
# case so a highly selective filter over a huge match set can't scan unbounded.
FILTER_OVERFETCH_MULTIPLIER = 3
MAX_FILTER_OVERFETCH = 500


class MemoryStore(ProjectMixin, LinkMixin):
    OBSIDIAN_CONFIG = {
        "obsidian": "auto",
        "prompt": False,
        "spellcheck": True,
        "attachmentFolderPath": "notes",
        "newFileLocation": "root",
        "alwaysUpdateLinks": True,
    }

    @staticmethod
    def _init_obsidian_config(vault_dir: Path) -> None:
        """Create minimal .obsidian/ config so the vault folder is auto-detected
        as an Obsidian vault, and notes open in preview mode with spellcheck on."""
        obs_dir = vault_dir / ".obsidian"
        if obs_dir.is_dir():
            return  # already configured
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "app.json").write_text(
            json.dumps(MemoryStore.OBSIDIAN_CONFIG, indent=2), encoding="utf-8"
        )
        # workspace file: minimum needed for Obsidian to treat this as a vault.
        (obs_dir / "workspace.json").write_text(
            json.dumps({"main": {"id": "main", "type": "split", "children": []}}), encoding="utf-8"
        )

    def __init__(self, vault_dir: str | Path, embedder=None, key=None):
        """`embedder` is optional: any object with `embed_one(str) -> list[float]`.
        When present, memories are embedded on write and `search` becomes a hybrid
        of keyword (FTS) + vector similarity. When absent, behaviour is unchanged.

        `key` opens an *encrypted* vault live ("locked mode"): notes are read/written
        as `<id>.md.enc`, and the search index lives only **in memory** — so the vault
        stays encrypted on disk yet is fully queryable and writable. Opening an
        encrypted vault without a key is refused (it would otherwise write plaintext).

        Memory footprint (locked mode): because the index cannot be cached to disk
        without leaking plaintext, the *entire* decrypted index is held in RAM for
        the life of the handle — roughly the size of all note bodies plus SQLite/FTS
        overhead (and, with an embedder, the vectors: ~1.5 KB per memory for a
        384-dim float32 model). Order-of-magnitude: a 10k-note vault of ~1 KB notes
        is tens of MB — fine on any desktop, but it scales linearly, so a very large
        encrypted vault on a memory-constrained host should expect that cost. A
        plaintext vault has no such ceiling: its index is the on-disk `index.db`."""
        self.vault_dir = Path(vault_dir).expanduser()
        self.notes_dir = self.vault_dir / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self._init_obsidian_config(self.vault_dir)
        self.embedder = embedder
        self.key = key
        vault_is_encrypted = crypto.is_encrypted(self.vault_dir)
        if vault_is_encrypted and not key:
            raise PermissionError(
                "vault is encrypted — a key is required to open it (run `ele decrypt`, "
                "or supply the passphrase)"
            )
        self.encrypted = bool(key) and vault_is_encrypted
        self._ensure_meta()
        # In locked mode the index is in-memory only — never persisted in plaintext.
        self.db_path = ":memory:" if self.encrypted else str(self.vault_dir / "index.db")
        # check_same_thread=False so the long-running MCP server can serve tool
        # calls that FastMCP may dispatch on worker threads; a single re-entrant
        # lock serialises every DB touch so the shared connection stays safe.
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # WAL mode + busy timeout so concurrent CLI/MCP-server processes don't
        # hit SQLITE_BUSY when they both touch the index at the same time.
        # Skip for in-memory (encrypted locked-mode) — no file-level concurrency.
        if not self.encrypted:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
        self._fts = self._init_schema()
        self._load_or_create_projects_manifest()
        with self._lock:
            if self._ensure_project_events_unsafe():
                self._write_projects_manifest_unsafe()
        if self.encrypted:
            # Decrypt every note into the in-memory index at startup. This is
            # inherent to encryption-at-rest, not a missed optimisation: the
            # index cannot be cached to disk (a plaintext index would leak the
            # very content encryption protects), so there is nothing to load
            # incrementally — the searchable form only ever exists in memory and
            # must be rebuilt from the ciphertext each time the vault is opened.
            self.reindex()

    # -- vault metadata --------------------------------------------------
    def _ensure_meta(self) -> None:
        """Create or validate the vault metadata file (meta.json) with a schema
        version, so future versions can detect and migrate incompatible layouts."""
        meta_path = self.vault_dir / META_FILE
        meta: dict = {"elesync_version": VAULT_SCHEMA_VERSION}
        existing_text: str | None = None
        if meta_path.exists():
            try:
                existing_text = meta_path.read_text(encoding="utf-8")
                meta = json.loads(existing_text)
                ver = meta.get("elesync_version", 0)
                if ver > VAULT_SCHEMA_VERSION:
                    print(
                        f"Warning: this vault was created by a newer EleSync version "
                        f"(schema v{ver}); this version supports up to v{VAULT_SCHEMA_VERSION}. "
                        f"Consider upgrading EleSync.",
                        file=__import__("sys").stderr,
                    )
            except (json.JSONDecodeError, OSError):
                meta = {"elesync_version": VAULT_SCHEMA_VERSION}
        # Skip the write when nothing changed: every CLI/MCP process opens the
        # vault, and a needless synchronous write here sits on the MCP startup
        # path and makes concurrent processes contend for the same file.
        new_text = json.dumps(meta, indent=2) + "\n"
        if new_text != existing_text:
            meta_path.write_text(new_text, encoding="utf-8")

    # -- note I/O (transparently encrypted in locked mode) ----------------
    def _note_path(self, mem_id: str) -> Path:
        return self.notes_dir / (f"{mem_id}.md.enc" if self.encrypted else f"{mem_id}.md")

    def _note_paths(self) -> list[Path]:
        return sorted(self.notes_dir.glob("*.md.enc" if self.encrypted else "*.md"))

    def _write_note(self, item: MemoryItem) -> None:
        text = item.to_markdown()
        path = self._note_path(item.id)
        if self.encrypted:
            path.write_bytes(crypto.encrypt(self.key, text.encode("utf-8")))
        else:
            path.write_text(text, encoding="utf-8")

    def _read_note(self, path: Path) -> str:
        if self.encrypted:
            return crypto.decrypt(self.key, path.read_bytes()).decode("utf-8")
        return path.read_text(encoding="utf-8")

    # -- durable project manifest ---------------------------------------
    def _projects_path(self) -> Path:
        name = PROJECTS_ENCRYPTED_FILE if self.encrypted else PROJECTS_FILE
        return self.vault_dir / name

    def _project_snapshot_unsafe(self) -> dict:
        projects = [dict(row) for row in self.db.execute("SELECT * FROM projects ORDER BY id")]
        memberships = [
            dict(row)
            for row in self.db.execute(
                "SELECT project_id, memory_id, added_at FROM project_memories "
                "ORDER BY project_id, memory_id"
            )
        ]
        documents = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM project_documents ORDER BY project_id, updated_at, id"
            )
        ]
        access = [
            dict(row)
            for row in self.db.execute("SELECT * FROM project_access ORDER BY project_id, provider")
        ]
        events = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM project_events ORDER BY project_id, created_at, id"
            )
        ]
        return {
            "format_version": PROJECTS_FORMAT_VERSION,
            "projects": projects,
            "memberships": memberships,
            "documents": documents,
            "access": access,
            "events": events,
        }

    def project_snapshot(self) -> dict:
        """Portable project metadata used by the manifest and backup payloads."""
        with self._lock:
            return self._project_snapshot_unsafe()

    def _write_projects_manifest_unsafe(self) -> None:
        path = self._projects_path()
        tmp = path.with_name(path.name + ".tmp")
        raw = (json.dumps(self._project_snapshot_unsafe(), indent=2) + "\n").encode("utf-8")
        if self.encrypted:
            tmp.write_bytes(crypto.encrypt(self.key, raw))
        else:
            tmp.write_bytes(raw)
        tmp.replace(path)

    @staticmethod
    def _manifest_has_stale_lifecycle(payload: dict) -> bool:
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, list):
            return False
        for document in documents:
            if not isinstance(document, dict):
                continue
            stored = (document.get("status"), document.get("completed_at"))
            if stored != _document_lifecycle(document.get("kind", "knowledge"), *stored):
                return True
        return False

    def _load_or_create_projects_manifest(self) -> None:
        """Restore projects from their durable file, or migrate the old DB-only form."""
        path = self._projects_path()
        if not path.exists():
            self._write_projects_manifest_unsafe()
            return
        try:
            raw = path.read_bytes()
            if self.encrypted:
                raw = crypto.decrypt(self.key, raw)
            payload = json.loads(raw.decode("utf-8"))
            self._restore_projects_unsafe(payload, replace=True)
            if self._manifest_has_stale_lifecycle(payload):
                # Persist the normalized lifecycle so the durable manifest stops
                # carrying task fields on non-task documents.
                self._write_projects_manifest_unsafe()
        except Exception as exc:
            # Preserve a usable index and the unreadable file for recovery.
            _log.warning(
                "could not load project manifest %s: %s: %s",
                path.name,
                type(exc).__name__,
                exc,
            )

    def _restore_projects_unsafe(self, payload: dict, replace: bool = False) -> int:
        if not isinstance(payload, dict) or payload.get("format_version") not in {
            1,
            PROJECTS_FORMAT_VERSION,
        }:
            raise ValueError("unsupported project manifest")
        projects = payload.get("projects", [])
        memberships = payload.get("memberships", [])
        documents = payload.get("documents", [])
        access = payload.get("access", [])
        events = payload.get("events", [])
        if (
            not isinstance(projects, list)
            or not isinstance(memberships, list)
            or not isinstance(documents, list)
            or not isinstance(access, list)
            or not isinstance(events, list)
        ):
            raise ValueError("invalid project manifest")
        if replace:
            self.db.execute("DELETE FROM project_memories")
            self.db.execute("DELETE FROM projects")
        added = 0
        for project in projects:
            if not isinstance(project, dict) or not project.get("id") or not project.get("name"):
                continue
            cur = self.db.execute(
                """INSERT OR IGNORE INTO projects
                   (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)""",
                (
                    str(project["id"]),
                    str(project["name"]),
                    str(project.get("description", "")),
                    str(project.get("created_at", "")),
                    str(project.get("updated_at", "")),
                ),
            )
            added += cur.rowcount
        for membership in memberships:
            if not isinstance(membership, dict):
                continue
            project_id = str(membership.get("project_id", ""))
            memory_id = str(membership.get("memory_id", ""))
            if not project_id or not memory_id:
                continue
            if not self.db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO project_memories (project_id, memory_id, added_at) "
                "VALUES (?, ?, ?)",
                (project_id, memory_id, str(membership.get("added_at", ""))),
            )
        for document in documents:
            if not isinstance(document, dict):
                continue
            document_id = document.get("id", "")
            project_id = document.get("project_id", "")
            title = document.get("title", "")
            body = document.get("body", "")
            kind = document.get("kind", "knowledge")
            if (
                not all(
                    isinstance(value, str) for value in (document_id, project_id, title, body, kind)
                )
                or not document_id
                or not project_id
                or not title
                or not body
                or kind not in {"knowledge", "task", "decision", "handoff"}
                or not self.db.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
            ):
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO project_documents "
                "(id, project_id, title, body, kind, status, completed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    project_id,
                    title,
                    body,
                    kind,
                    *_document_lifecycle(
                        kind, document.get("status"), document.get("completed_at")
                    ),
                    str(document.get("created_at", "")),
                    str(document.get("updated_at", "")),
                ),
            )
        for entry in access:
            if not isinstance(entry, dict):
                continue
            project_id = str(entry.get("project_id", ""))
            provider = str(entry.get("provider", ""))
            if (
                not project_id
                or not provider
                or not self.db.execute(
                    "SELECT 1 FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
            ):
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO project_access "
                "(project_id, provider, can_read, can_write, can_read_conversations, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    provider,
                    int(bool(entry.get("can_read", True))),
                    int(bool(entry.get("can_write", False))),
                    int(bool(entry.get("can_read_conversations", False))),
                    str(entry.get("status", "active")),
                    str(entry.get("created_at", "")),
                    str(entry.get("updated_at", "")),
                ),
            )
        for event in events:
            if not isinstance(event, dict):
                continue
            values = (
                str(event.get("id", "")),
                str(event.get("project_id", "")),
                str(event.get("event_type", "")),
                str(event.get("subject_id", "")),
                str(event.get("actor", "")),
                str(event.get("title", "")),
                str(event.get("detail", "")),
                str(event.get("created_at", "")),
            )
            if not all(values[index] for index in (0, 1, 2, 4, 5, 7)):
                continue
            if not self.db.execute("SELECT 1 FROM projects WHERE id = ?", (values[1],)).fetchone():
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO project_events "
                "(id, project_id, event_type, subject_id, actor, title, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        self.db.commit()
        return added

    def restore_projects(self, payload: dict | None) -> int:
        """Merge projects from a backup, preserving local projects and memories."""
        if not payload:
            return 0
        with self._lock:
            added = self._restore_projects_unsafe(payload)
            self._write_projects_manifest_unsafe()
            return added

    # -- schema -----------------------------------------------------------
    @staticmethod
    def _migrate_project_document_lifecycle(cur) -> None:
        """Keep task lifecycle fields (status, completed_at) on tasks only.

        Older vaults declared ``status TEXT NOT NULL DEFAULT 'open'``, which
        stamped every knowledge, decision, and handoff document with a task
        status. SQLite cannot relax a NOT NULL constraint in place, so such a
        table is rebuilt once with a nullable column. The normalization below
        is idempotent and runs on every open, so documents restored or written
        by older builds are corrected too.
        """
        columns = {
            row[1]: row for row in cur.execute("PRAGMA table_info(project_documents)").fetchall()
        }
        if "status" not in columns:
            cur.execute("ALTER TABLE project_documents ADD COLUMN status TEXT")
        elif columns["status"][3]:  # notnull flag from the legacy schema
            has_completed = "completed_at" in columns
            cur.execute(
                """CREATE TABLE project_documents_lifecycle (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'knowledge',
                    status TEXT, completed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )"""
            )
            cur.execute(
                "INSERT INTO project_documents_lifecycle "
                "(id, project_id, title, body, kind, status, completed_at, created_at, updated_at) "
                "SELECT id, project_id, title, body, kind, status, "
                + ("completed_at" if has_completed else "NULL")
                + ", created_at, updated_at FROM project_documents"
            )
            cur.execute("DROP TABLE project_documents")
            cur.execute("ALTER TABLE project_documents_lifecycle RENAME TO project_documents")
            columns = {
                row[1]: row
                for row in cur.execute("PRAGMA table_info(project_documents)").fetchall()
            }
        if "completed_at" not in columns:
            cur.execute("ALTER TABLE project_documents ADD COLUMN completed_at TEXT")
        cur.execute(
            "UPDATE project_documents SET status = NULL, completed_at = NULL "
            "WHERE kind != 'task' AND (status IS NOT NULL OR completed_at IS NOT NULL)"
        )
        cur.execute(
            "UPDATE project_documents SET status = 'open' "
            "WHERE kind = 'task' AND (status IS NULL OR status NOT IN ('open', 'completed'))"
        )
        cur.execute(
            "UPDATE project_documents SET completed_at = NULL "
            "WHERE kind = 'task' AND status = 'open' AND completed_at IS NOT NULL"
        )

    def _init_schema(self) -> bool:
        cur = self.db.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT, type TEXT, tags TEXT,
                confidence REAL, created_at TEXT,
                source_ref TEXT, content_hash TEXT UNIQUE,
                embedding BLOB, scopes TEXT,
                mtime REAL
            )"""
        )
        # Migrate vaults created before semantic recall / scoped sharing existed.
        cols = [r[1] for r in cur.execute("PRAGMA table_info(memories)")]
        if "embedding" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
        if "scopes" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN scopes TEXT")
        if "mtime" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN mtime REAL")
        # Supersession: a memory whose `superseded_by` points at a newer one is
        # retained history, hidden from live recall but recoverable and walkable.
        if "superseded_by" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT")
        if "superseded_at" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN superseded_at TEXT")
        # Direct links ("relation:target, ...") mirrored from the note so every
        # read-modify-write of an item keeps them (see store_links.py).
        if "links" not in cols:
            cur.execute("ALTER TABLE memories ADD COLUMN links TEXT")
        # Conflict resolution tracking table.
        cur.execute(
            """CREATE TABLE IF NOT EXISTS conflict_resolutions (
                cluster_id TEXT NOT NULL,
                mem_id_a TEXT NOT NULL,
                mem_id_b TEXT NOT NULL,
                resolved_at TEXT NOT NULL,
                kept_id TEXT,
                strategy TEXT,
                PRIMARY KEY (mem_id_a, mem_id_b)
            )"""
        )
        # Projects are lightweight collections over memories. Keeping links in
        # their own table means one imported chat can support more than one
        # project without copying it or losing its original source.
        cur.execute(
            """CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        project_columns = {row[1] for row in cur.execute("PRAGMA table_info(projects)").fetchall()}
        if "status" not in project_columns:
            cur.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        cur.execute(
            """CREATE TABLE IF NOT EXISTS project_memories (
                project_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (project_id, memory_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS project_documents (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'knowledge',
                status TEXT, completed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )"""
        )
        self._migrate_project_document_lifecycle(cur)
        cur.execute(
            """CREATE TABLE IF NOT EXISTS project_access (
                project_id TEXT NOT NULL, provider TEXT NOT NULL,
                can_read INTEGER NOT NULL DEFAULT 1,
                can_write INTEGER NOT NULL DEFAULT 0,
                can_read_conversations INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, provider),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS project_events (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
                subject_id TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS project_events_project_created "
            "ON project_events(project_id, created_at DESC, id DESC)"
        )
        fts = False
        try:
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(content, tags, content='memories', content_rowid='rowid')"
            )
            # keep FTS in sync
            cur.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                  INSERT INTO memories_fts(rowid, content, tags)
                  VALUES (new.rowid, new.content, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                  VALUES('delete', old.rowid, old.content, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                  VALUES('delete', old.rowid, old.content, old.tags);
                  INSERT INTO memories_fts(rowid, content, tags)
                  VALUES (new.rowid, new.content, new.tags);
                END;
                """
            )
            fts = True
        except sqlite3.OperationalError:
            fts = False  # FTS5 not compiled in; fall back to LIKE search
        self.db.commit()
        return fts

    # -- writes -----------------------------------------------------------
    def _insert(self, item: MemoryItem, write_note: bool = True) -> bool:
        """Low-level insert. Returns True if a new row was written, False if an
        identical memory (same content_hash) already existed. Caller holds the lock."""
        emb = embeddings.pack(self.embedder.embed_one(item.content)) if self.embedder else None
        cur = self.db.execute(
            """INSERT OR IGNORE INTO memories
               (id, content, source, type, tags, confidence, created_at, source_ref,
                content_hash, embedding, scopes, superseded_by, superseded_at, links)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.id,
                item.content,
                item.source,
                item.type,
                ", ".join(item.tags),
                item.confidence,
                item.created_at,
                item.source_ref,
                item.content_hash,
                emb,
                ", ".join(item.scopes),
                item.superseded_by or None,
                item.superseded_at or None,
                ", ".join(item.links) or None,
            ),
        )
        inserted = cur.rowcount > 0
        if inserted and write_note:
            # Notes are the source of truth. Write the note first and only
            # commit the DB row on success — otherwise a failed note write
            # would leave the row in the index with no backing file, and the
            # memory would silently disappear on the next reindex.
            try:
                self._write_note(item)
            except Exception:
                self.db.rollback()
                raise
        self.db.commit()
        return inserted

    def upsert_status(self, item: MemoryItem) -> tuple[str, bool]:
        """Insert, or skip if an identical memory (same content_hash) exists.
        Returns (id, inserted) — id is the existing one on a dedup hit."""
        with self._lock:
            existing = self.db.execute(
                "SELECT id FROM memories WHERE content_hash = ?", (item.content_hash,)
            ).fetchone()
            if existing:
                return existing["id"], False
            self._insert(item)
            return item.id, True

    def upsert(self, item: MemoryItem) -> str:
        """Insert, or skip if an identical memory exists. Returns the stored id."""
        return self.upsert_status(item)[0]

    def import_many(
        self, items: list[MemoryItem], progress=None, project_id: str | None = None
    ) -> dict:
        """Import many memories, deduping by content hash.

        `progress`, if given, is called as ``progress(done, total)`` after each
        item so a caller (the CLI) can render a progress bar for a large import.
        It defaults to None — the library and the MCP server stay silent; only
        the interactive CLI opts in."""
        added = skipped = 0
        stored_ids = []
        total = len(items)
        with self._lock:
            if project_id is not None and not self.project(project_id):
                raise ValueError("project not found")
            for n, it in enumerate(items, 1):
                stored_id, inserted = self.upsert_status(it)
                stored_ids.append(stored_id)
                added += inserted
                skipped += not inserted
                if progress is not None:
                    progress(n, total)
            if project_id is not None:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                self.db.executemany(
                    "INSERT OR IGNORE INTO project_memories (project_id, memory_id, added_at) VALUES (?, ?, ?)",
                    [(project_id, memory_id, now) for memory_id in stored_ids],
                )
                self.db.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
                )
                self.db.commit()
                self._write_projects_manifest_unsafe()
            return {
                "added": added,
                "skipped_duplicates": skipped,
                "total": self.count(),
                "project_id": project_id,
            }

    def _reindex_unsafe(self, progress=None) -> dict:
        """Rebuild the index from notes. Caller must hold self._lock.

        `progress(done, total)` is invoked per note when provided (see
        `import_many` for the opt-in rationale)."""
        self.db.execute("DELETE FROM memories")
        self.db.commit()
        count = 0
        notes = self._note_paths()
        total = len(notes)
        for n, note in enumerate(notes, 1):
            if progress is not None:
                progress(n, total)
            try:
                item = MemoryItem.from_markdown(self._read_note(note))
            except Exception as exc:
                # skip unparseable/undecryptable files; don't abort the rebuild.
                # Log so a corrupt note or a wrong-passphrase decrypt isn't invisible.
                _log.warning(
                    "skipping unparseable note %s: %s: %s", note.name, type(exc).__name__, exc
                )
                continue
            if self._insert(item, write_note=False):
                # Store mtime so a later incremental sync can skip unchanged files.
                # Skip it in encrypted (locked) mode: the index is in-memory and
                # discarded on close, so a persisted mtime can never be read back —
                # writing it would only add a stat() + UPDATE per note to the one
                # startup path (decrypt-and-rebuild) we most want to keep lean.
                if not self.encrypted:
                    try:
                        self.db.execute(
                            "UPDATE memories SET mtime = ? WHERE id = ?",
                            (note.stat().st_mtime, item.id),
                        )
                    except Exception as exc:
                        # Don't abort the rebuild over a single mtime write, but
                        # surface it — a persistent failure here means incremental
                        # sync silently stops working and the user sees stale
                        # results with no clue why.
                        _log.warning(
                            "mtime update failed for %s: %s: %s", note.name, type(exc).__name__, exc
                        )
                count += 1
        self.db.execute(
            "DELETE FROM project_memories WHERE memory_id NOT IN (SELECT id FROM memories)"
        )
        self.db.commit()
        self._write_projects_manifest_unsafe()
        return {"reindexed": count, "vault_dir": str(self.vault_dir)}

    def reindex(self, progress=None) -> dict:
        """Rebuild the index from the notes — the real source of truth.

        Makes 'just sync the files' work: after a git pull / Obsidian edit on
        another machine, this re-derives the searchable index from the notes so
        the vault and its index agree again. Ids and content hashes are preserved
        from each note's front-matter, so it's a faithful rebuild, not a reimport.
        In locked mode it decrypts the notes into the in-memory index.

        `progress(done, total)` is called per note when provided, so a long
        rebuild can show a progress bar (see `import_many`)."""
        with self._lock:
            return self._reindex_unsafe(progress)

    def sync(self, progress=None) -> dict:
        """Reconcile the index with the notes on disk and report what changed since
        the last index — for after you sync the vault folder across devices
        (git / iCloud / Syncthing / Dropbox). Notes are the source of truth, so this
        is always safe and idempotent; it just rebuilds the local cache and tells you
        what moved (added / changed / removed)."""
        with self._lock:
            before = {
                r["id"]: r["content"] for r in self.db.execute("SELECT id, content FROM memories")
            }
            self._reindex_unsafe(progress)
            after = {
                r["id"]: r["content"] for r in self.db.execute("SELECT id, content FROM memories")
            }
        added = [i for i in after if i not in before]
        removed = [i for i in before if i not in after]
        changed = [i for i in after if i in before and after[i] != before[i]]
        return {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "total": len(after),
            "vault_dir": str(self.vault_dir),
        }

    def embed_missing(self, batch: int = 64) -> int:
        """Compute and store embeddings for any memories that lack one. Used by
        `ele embed` to turn on semantic recall for an existing vault (and to
        backfill after importing while semantic mode was off). Returns the count."""
        if not self.embedder:
            raise RuntimeError("no embedder configured; install the 'semantic' extra")
        with self._lock:
            rows = self.db.execute(
                "SELECT id, content FROM memories WHERE embedding IS NULL"
            ).fetchall()
            for i in range(0, len(rows), batch):
                chunk = rows[i : i + batch]
                vecs = self.embedder.embed([r["content"] for r in chunk])
                for r, v in zip(chunk, vecs, strict=True):
                    self.db.execute(
                        "UPDATE memories SET embedding = ? WHERE id = ?",
                        (embeddings.pack(v), r["id"]),
                    )
                self.db.commit()
            return len(rows)

    def prefix_lookup(self, prefix: str) -> list[str]:
        """Return all memory ids that start with `prefix`. Used by `forget` to
        resolve an 8-char shorthand without loading every memory into Python."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id FROM memories WHERE id LIKE ?", (f"{prefix}%",)
            ).fetchall()
        return [r["id"] for r in rows]

    def update(self, item: MemoryItem) -> bool:
        """Update an existing memory without disturbing project membership.

        The memory row and its portable note stay in lock-step.  A content-hash
        collision is reported instead of silently replacing either memory.
        """
        with self._lock:
            existing = self.db.execute(
                "SELECT content_hash, embedding FROM memories WHERE id = ?", (item.id,)
            ).fetchone()
            if existing is None:
                return False
            if existing["content_hash"] == item.content_hash:
                embedding = existing["embedding"]
            else:
                embedding = (
                    embeddings.pack(self.embedder.embed_one(item.content))
                    if self.embedder
                    else None
                )
            try:
                self.db.execute(
                    """UPDATE memories SET
                       content = ?, source = ?, type = ?, tags = ?, confidence = ?,
                       created_at = ?, source_ref = ?, content_hash = ?, embedding = ?,
                       scopes = ?, superseded_by = ?, superseded_at = ?, links = ?
                       WHERE id = ?""",
                    (
                        item.content,
                        item.source,
                        item.type,
                        ", ".join(item.tags),
                        item.confidence,
                        item.created_at,
                        item.source_ref,
                        item.content_hash,
                        embedding,
                        ", ".join(item.scopes),
                        item.superseded_by or None,
                        item.superseded_at or None,
                        ", ".join(item.links) or None,
                        item.id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self.db.rollback()
                raise ValueError("an identical memory already exists") from exc
            # Write the note before committing so a failed note write rolls back
            # the index update — otherwise reindex from notes would silently
            # revert the change.
            try:
                self._write_note(item)
            except Exception:
                self.db.rollback()
                raise
            self.db.commit()
            return True

    def delete(self, mem_id: str) -> bool:
        with self._lock:
            cur = self.db.cursor()
            row = cur.execute("SELECT id FROM memories WHERE id = ?", (mem_id,)).fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM project_memories WHERE memory_id = ?", (mem_id,))
            cur.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            self.db.commit()
            self._drop_links_to_unsafe(mem_id)
            self._write_projects_manifest_unsafe()
            f = self._note_path(mem_id)
            if f.exists():
                f.unlink()
            return True

    def supersede(self, old_id: str, new_id: str, at: str) -> bool:
        """Mark ``old_id`` as replaced by ``new_id``: retain it as recoverable
        history (hidden from live recall) rather than deleting it. Updates both
        the SQLite index and the markdown note (source of truth), mirroring
        ``set_scopes``. Returns False if ``old_id`` doesn't exist."""
        with self._lock:
            item = self.get(old_id)
            if item is None:
                return False
            item.superseded_by = new_id
            item.superseded_at = at
            self.db.execute(
                "UPDATE memories SET superseded_by = ?, superseded_at = ? WHERE id = ?",
                (new_id, at, old_id),
            )
            try:
                self._write_note(item)  # keep the note (source of truth) in lock-step
            except Exception:
                self.db.rollback()
                raise
            self.db.commit()
            return True

    def predecessors(self, mem_id: str) -> list[MemoryItem]:
        """Memories that were superseded *by* ``mem_id`` — its immediate
        predecessors in the supersession chain."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM memories WHERE superseded_by = ?", (mem_id,)
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def set_scopes(self, mem_id: str, scopes) -> bool:
        """Re-scope an existing memory in place. Scopes are *not* part of the
        content hash, so this changes which clients can see the memory without
        forking its identity or creating a duplicate. Updates both the SQLite
        index and the markdown note (re-encrypting it in locked mode). Returns
        False if no such memory."""
        labels = scopes_mod.parse(scopes)
        with self._lock:
            item = self.get(mem_id)
            if item is None:
                return False
            item.scopes = labels
            self.db.execute(
                "UPDATE memories SET scopes = ? WHERE id = ?",
                (", ".join(labels), mem_id),
            )
            try:
                self._write_note(item)  # keep the note (source of truth) in lock-step
            except Exception:
                self.db.rollback()
                raise
            self.db.commit()
            return True

    def scope_counts(self, grant: scopes_mod.Grant | None = None) -> dict[str, int]:
        """How many memories carry each scope label (``general`` = unscoped). When
        a `grant` is given, only memories that grant can see are counted, and only
        labels that grant can see are reported — a scoped client can't enumerate
        compartments it has no access to."""
        counts: dict[str, int] = {}
        for it in self.all():
            if grant is not None and not scopes_mod.is_visible(it.scopes, grant):
                continue
            labels = it.scopes or [scopes_mod.GENERAL]
            for label in labels:
                if grant is not None and label != scopes_mod.GENERAL and label not in grant:
                    continue
                counts[label] = counts.get(label, 0) + 1
        return counts

    # -- reads ------------------------------------------------------------
    def get(self, mem_id: str) -> MemoryItem | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
        return self._row_to_item(row) if row else None

    def search(
        self,
        query: str | SearchQuery,
        limit: int = 10,
        source: str | None = None,
        mtype: str | None = None,
        exclude_type: str | None = None,
        grant: scopes_mod.Grant | None = None,
        include_superseded: bool = False,
        semantic_weight: float | None = None,
        created_after: str | None = None,
    ) -> list[MemoryItem]:
        """`grant` (a client's allowed scopes, or None for unrestricted) hides
        memories the caller isn't allowed to see — the per-client compartment.
        Superseded memories are hidden from recall by default (they are retained
        history, not live facts); pass `include_superseded=True` to see them.

        ``semantic_weight`` tunes hybrid (FTS + vector) search: 0.0 = pure
        keyword, 1.0 = pure semantic, 0.5 = equal weight (default). Ignored
        when no embeddings are available (falls back to FTS). ``None`` means
        use the default (0.5); values outside [0.0, 1.0] are clamped."""
        if isinstance(query, SearchQuery):
            search_query = query
            query = search_query.text
            limit = search_query.limit
            source = search_query.source
            mtype = search_query.memory_type
            exclude_type = search_query.exclude_type
            grant = search_query.grant
            include_superseded = search_query.include_superseded
            semantic_weight = search_query.semantic_weight
            created_after = search_query.created_after
        else:
            search_query = SearchQuery(
                text=query,
                limit=limit,
                source=source,
                memory_type=mtype,
                exclude_type=exclude_type,
                grant=grant,
                include_superseded=include_superseded,
                semantic_weight=semantic_weight,
                created_after=created_after,
            )
            limit = search_query.limit
            semantic_weight = search_query.semantic_weight

        post_filtered = bool(
            source
            or mtype
            or exclude_type
            or grant is not None
            or not include_superseded
            or created_after
        )

        def _apply_filters(items: list[MemoryItem]) -> list[MemoryItem]:
            if not include_superseded:
                items = [i for i in items if not i.superseded_by]
            if source:
                items = [i for i in items if i.source == source]
            if mtype:
                items = [i for i in items if i.type == mtype]
            if exclude_type:
                items = [i for i in items if i.type != exclude_type]
            if created_after:
                items = [i for i in items if i.created_at >= created_after]
            if grant is not None:
                items = [i for i in items if scopes_mod.is_visible(i.scopes, grant)]
            return items

        def _fetch(n: int) -> list:
            with self._lock:
                if self.embedder and query.strip() and self._has_embeddings():
                    return self._hybrid_rows(query, n, semantic_weight=semantic_weight)
                return self._search_rows(query, n)

        # No post-filtering: the FTS/vector LIMIT is exact, so one fetch suffices.
        if not post_filtered:
            return [self._row_to_item(r) for r in _fetch(limit)]

        # Post-filtering: over-fetch, then widen adaptively if a selective filter
        # left us short. Widening stops once we have `limit`, once the underlying
        # search is exhausted (a fetch returns fewer rows than asked — no more
        # candidates exist), or once we hit MAX_FILTER_OVERFETCH.
        fetch = min(limit * FILTER_OVERFETCH_MULTIPLIER, MAX_FILTER_OVERFETCH)
        while True:
            rows = _fetch(fetch)
            items = _apply_filters([self._row_to_item(r) for r in rows])
            if len(items) >= limit or len(rows) < fetch or fetch >= MAX_FILTER_OVERFETCH:
                return items[:limit]
            fetch = min(fetch * 2, MAX_FILTER_OVERFETCH)

    def _has_embeddings(self) -> bool:
        return (
            self.db.execute("SELECT 1 FROM memories WHERE embedding IS NOT NULL LIMIT 1").fetchone()
            is not None
        )

    def embeddings_dict(self) -> dict[str, list[float]]:
        """Return ``{id: vector}`` for every memory that has an embedding.

        Used by conflict detection to reuse the same semantic signal the
        semantic recall already depends on — no new
        datastore, just a read of the vectors already sitting in the index.
        Returns an empty dict when no embeddings exist (caller falls back
        to the lexical heuristic).
        """
        out: dict[str, list[float]] = {}
        with self._lock:
            rows = self.db.execute(
                "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL"
            ).fetchall()
        for row in rows:
            try:
                out[row["id"]] = embeddings.unpack(row["embedding"])
            except (ValueError, OSError):
                continue  # malformed blob — skip; `ele embed` fixes the state
        return out

    def _vector_rows(self, query: str, limit: int):
        """Rows ranked by cosine similarity of the query embedding (best first).

        Lock-free: callers must hold self._lock before calling this method.
        """
        qvec = self.embedder.embed_one(query)
        scored: list[tuple[float, str]] = []
        for r in self.db.execute("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL"):
            scored.append((embeddings.cosine(qvec, embeddings.unpack(r["embedding"])), r["id"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        top_ids = [mid for _, mid in scored[:limit]]
        if not top_ids:
            return []
        placeholders = ",".join("?" for _ in top_ids)
        rows = self.db.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",
            top_ids,
        ).fetchall()
        rowmap = {r["id"]: r for r in rows}
        return [rowmap[mid] for mid in top_ids if mid in rowmap]

    def _hybrid_rows(self, query: str, limit: int, semantic_weight: float | None = None):
        """Fuse keyword (FTS) and vector rankings with Reciprocal Rank Fusion, so
        exact-keyword hits stay strong while semantically-close memories surface
        even when they share no words with the query.

        ``semantic_weight`` in [0.0, 1.0] controls the blend: 0.0 = pure FTS,
        1.0 = pure vector, 0.5 = equal (default). The FTS weight is
        ``1 - semantic_weight``. ``None`` means 0.5."""
        sw = semantic_weight if semantic_weight is not None else 0.5
        # Clamp to [0.0, 1.0] so a misconfigured value can't invert the ranking.
        sw = max(0.0, min(1.0, sw))
        fw = 1.0 - sw
        k = 60
        scores: dict[str, float] = {}
        rowmap: dict = {}
        # (rows, weight) pairs — pure FTS when sw=0, pure vector when sw=1.
        ranked_lists = [
            (self._search_rows(query, limit), fw),
            (self._vector_rows(query, limit), sw),
        ]
        for ranked, weight in ranked_lists:
            for rank, r in enumerate(ranked):
                scores[r["id"]] = scores.get(r["id"], 0.0) + weight / (k + rank + 1)
                rowmap[r["id"]] = r
        order = sorted(scores, key=lambda i: scores[i], reverse=True)
        return [rowmap[i] for i in order[:limit]]

    def _search_rows(self, query: str, limit: int):
        q = query.strip()
        if not q:
            return self.db.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        if self._fts:
            # quote terms so punctuation in the query can't break FTS syntax;
            # double any internal quotes so they're valid FTS5 phrase literals.
            match = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in q.split())
            try:
                return self.db.execute(
                    """SELECT m.* FROM memories m
                       JOIN memories_fts f ON m.rowid = f.rowid
                       WHERE memories_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return self.db.execute(
                    "SELECT * FROM memories WHERE content LIKE ? OR tags LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (f"%{q}%", f"%{q}%", limit),
                ).fetchall()
        return self.db.execute(
            "SELECT * FROM memories WHERE content LIKE ? OR tags LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()

    def page(
        self,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        mtype: str | None = None,
        exclude_type: str | None = None,
        include_superseded: bool = False,
        created_after: str | None = None,
        project_id: str | None = None,
    ) -> tuple[list[MemoryItem], int]:
        """One page of memories plus the total matching count, filtered in SQL.

        The browse endpoints used to call `all()` and slice the result, which
        meant loading and rebuilding every MemoryItem in the vault to display
        thirty of them — a cost that grew with the vault while the page size
        stayed the same. Here SQLite does the filtering, counting and slicing,
        so a request reads `limit` rows whether the vault holds a hundred
        memories or a hundred thousand.

        Ordering is `created_at DESC, id DESC`. The id tiebreaker is not
        cosmetic: `created_at` has second resolution, so a bulk import gives
        many rows the same timestamp, and without a deterministic tiebreaker
        LIMIT/OFFSET could show one memory on two consecutive pages and skip
        another entirely. `all()` never had to care because it returned
        everything at once.
        """
        where = []
        args: list = []
        if project_id:
            where.append("id IN (SELECT memory_id FROM project_memories WHERE project_id = ?)")
            args.append(project_id)
        if not include_superseded:
            where.append("(superseded_by IS NULL OR superseded_by = '')")
        if source:
            where.append("source = ?")
            args.append(source)
        if mtype:
            where.append("type = ?")
            args.append(mtype)
        if exclude_type:
            where.append("type != ?")
            args.append(exclude_type)
        if created_after:
            where.append("created_at >= ?")
            args.append(created_after)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM memories{clause}", args).fetchone()[0]
            rows = self.db.execute(
                f"SELECT * FROM memories{clause} ORDER BY created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                [*args, max(0, int(limit)), max(0, int(offset))],
            ).fetchall()
        return [self._row_to_item(r) for r in rows], int(total)

    def graph_memories(
        self, project_id: str = "", limit: int | None = None
    ) -> tuple[list[dict], int]:
        """Newest live graph records (up to `limit`) plus the total live count.

        Only a short preview is read, never entire conversation bodies."""
        where = "(superseded_by IS NULL OR superseded_by = '')"
        args: list = []
        if project_id:
            where += " AND id IN (SELECT memory_id FROM project_memories WHERE project_id = ?)"
            args.append(project_id)
        with self._lock:
            total = self.db.execute(
                f"SELECT COUNT(*) FROM memories WHERE {where}", args
            ).fetchone()[0]
            rows = self.db.execute(
                "SELECT id, source, substr(ltrim(content), 1, 200) AS preview, tags, links "
                f"FROM memories WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                [*args, -1 if limit is None else limit],
            ).fetchall()
        records = [
            {
                "id": row["id"],
                "source": row["source"],
                "label": row["preview"].strip().split("\n", 1)[0][:100] or "Untitled memory",
                "tags": [tag.strip() for tag in (row["tags"] or "").split(",") if tag.strip()],
                "links": normalize_links(row["links"] or ""),
            }
            for row in rows
        ]
        return records, total

    def all(self, include_superseded: bool = False) -> list[MemoryItem]:
        with self._lock:
            # Same `created_at DESC, id DESC` order as `page()`. The id tiebreaker
            # keeps the two paths agreeing: `created_at` has second resolution, so
            # a bulk import leaves many rows tied, and without it `all()` and a
            # paginated browse of the same vault would list them differently.
            rows = self.db.execute(
                "SELECT * FROM memories ORDER BY created_at DESC, id DESC"
            ).fetchall()
        items = [self._row_to_item(r) for r in rows]
        if not include_superseded:
            items = [i for i in items if not i.superseded_by]
        return items

    def count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

    # -- project methods live on ProjectMixin (store_projects.py) ----------

    def stats(self) -> dict:
        with self._lock:
            live = "WHERE superseded_by IS NULL OR superseded_by = ''"
            by_source = {
                r["source"]: r["c"]
                for r in self.db.execute(
                    f"SELECT source, COUNT(*) c FROM memories {live} GROUP BY source"
                )
            }
            by_type = {
                r["type"]: r["c"]
                for r in self.db.execute(
                    f"SELECT type, COUNT(*) c FROM memories {live} GROUP BY type"
                )
            }
            total = self.db.execute(f"SELECT COUNT(*) c FROM memories {live}").fetchone()["c"]
            superseded = self.db.execute(
                "SELECT COUNT(*) c FROM memories "
                "WHERE superseded_by IS NOT NULL AND superseded_by != ''"
            ).fetchone()["c"]
            embedded = self.db.execute(
                "SELECT COUNT(*) c FROM memories WHERE embedding IS NOT NULL"
            ).fetchone()["c"]
        return {
            "total": total,
            "superseded": superseded,
            "by_source": by_source,
            "by_type": by_type,
            "fts_enabled": self._fts,
            "embedded": embedded,
            "semantic": bool(self.embedder),
            "vault_dir": str(self.vault_dir),
        }

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        # `scopes` is absent on rows from a pre-1.1 vault until reindex; tolerate it.
        row_keys = row.keys()
        raw_scopes = row["scopes"] if "scopes" in row_keys else ""
        return MemoryItem.from_dict(
            {
                "id": row["id"],
                "content": row["content"],
                "source": row["source"],
                "type": row["type"],
                "tags": [t.strip() for t in (row["tags"] or "").split(",") if t.strip()],
                "scopes": [s.strip() for s in (raw_scopes or "").split(",") if s.strip()],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "source_ref": row["source_ref"],
                "content_hash": row["content_hash"],
                "superseded_by": (row["superseded_by"] if "superseded_by" in row_keys else "")
                or "",
                "superseded_at": (row["superseded_at"] if "superseded_at" in row_keys else "")
                or "",
                "links": (row["links"] if "links" in row_keys else "") or "",
            }
        )

    # -- conflict resolution ---------------------------------------------
    def resolved_pairs(self) -> set[frozenset]:
        """Return the set of resolved memory-id pairs as frozensets, so
        find_conflicts can skip them."""
        with self._lock:
            rows = self.db.execute("SELECT mem_id_a, mem_id_b FROM conflict_resolutions").fetchall()
        return {frozenset((r["mem_id_a"], r["mem_id_b"])) for r in rows}

    def record_resolution(
        self,
        cluster_id: str,
        pair_ids: list[tuple[str, str]],
        kept_id: str | None,
        strategy: str | None,
    ) -> None:
        """Persist a resolution record for all pairs in a cluster so they
        won't resurface in future conflict scans."""
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            for a, b in pair_ids:
                # Canonical order so (a,b) and (b,a) map to the same PK.
                lo, hi = (a, b) if a < b else (b, a)
                self.db.execute(
                    """INSERT OR REPLACE INTO conflict_resolutions
                       (cluster_id, mem_id_a, mem_id_b, resolved_at, kept_id, strategy)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (cluster_id, lo, hi, ts, kept_id, strategy),
                )
            self.db.commit()

    def close(self) -> None:
        # Hold the store lock so we wait for any in-flight DB operation to
        # finish before tearing the connection down, and stay idempotent so a
        # double close (e.g. serve() shutdown after a vault switch) is safe.
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            self.db.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
