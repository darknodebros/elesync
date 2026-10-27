"""Project/workspace application service.

This is the boundary for project workflows used by the web layer.  Persistence
stays in ``MemoryStore``; this service owns the project-level orchestration so
HTTP handlers do not need to know how project memories, documents, access, and
briefs are assembled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import MemoryStore


class ProjectService:
    """Coordinate project operations without changing the storage contract."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def list_projects(self) -> list[dict]:
        return self.store.projects()

    def get(self, project_id: str) -> dict | None:
        project = self.store.project(project_id)
        if project is None:
            return None
        project["memories"] = self.store.project_memories(project_id)
        project["documents"] = self.store.project_documents(project_id)
        return project

    def create(self, name: str, description: str = "") -> dict:
        return self.store.create_project(name, description, actor="Dashboard")

    def discover(self, limit: int = 5) -> dict:
        """Suggest projects from memories that are not yet in any project."""
        from .project_discovery import suggest_projects

        items = self.store.unfiled_memories()
        names = [project["name"] for project in self.store.projects()]
        suggestions = suggest_projects(
            items, existing_names=names, max_projects=max(1, min(int(limit), 8))
        )
        return {"suggestions": suggestions, "scanned": len(items)}

    def create_from_suggestion(self, name: str, description: str, memory_ids: list) -> dict:
        project = self.store.create_project(name, description, actor="Dashboard")
        project["memory_count"] = self.store.add_memories_to_project(
            project["id"], memory_ids, actor="Dashboard"
        )
        return project

    def update(self, project_id: str, name: str, description: str, status: str) -> dict | None:
        current = self.store.project(project_id)
        if current is None:
            return None
        return self.store.update_project(project_id, name, description, status, actor="Dashboard")

    def delete(self, project_id: str) -> bool:
        return self.store.delete_project(project_id)

    def add_memory(self, project_id: str, memory_id: str) -> bool:
        return self.store.add_to_project(project_id, memory_id, actor="Dashboard")

    def remove_memory(self, project_id: str, memory_id: str) -> bool:
        return self.store.remove_from_project(project_id, memory_id, actor="Dashboard")

    def search(self, project_id: str, query: str) -> list:
        return self.store.search_project(project_id, query)

    def context(self, project_id: str) -> dict:
        return self.store.project_context(project_id)

    def handoff(self, project_id: str) -> str:
        return self.store.project_handoff_prompt(project_id)

    def timeline(self, project_id: str) -> list[dict]:
        return self.store.project_timeline(project_id)

    def suggestions(self, project_id: str) -> list[dict]:
        return self.store.project_suggestions(project_id)

    def access(self, project_id: str) -> list[dict]:
        return self.store.project_access(project_id)

    def set_access(
        self,
        project_id: str,
        provider: str,
        can_read: bool,
        can_write: bool,
        can_read_conversations: bool,
    ) -> dict:
        return self.store.set_project_access(
            project_id, provider, can_read, can_write, can_read_conversations, actor="Dashboard"
        )

    def revoke_access(self, project_id: str, provider: str) -> bool:
        revoked = self.store.revoke_project_access(project_id, provider, actor="Dashboard")
        if revoked:
            from . import oauth

            oauth.revoke_project_authorizations(self.store.vault_dir, project_id, provider)
        return revoked

    def save_document(
        self, project_id: str, title: str, body: str, kind: str = "knowledge"
    ) -> dict:
        return self.store.save_project_document(project_id, title, body, kind, actor="Dashboard")

    def update_document(
        self, project_id: str, document_id: str, title: str, body: str, kind: str
    ) -> dict | None:
        document = self.store.project_document(document_id)
        if document is None or document["project_id"] != project_id:
            return None
        return self.store.update_project_document(document_id, title, body, kind, actor="Dashboard")

    def delete_document(self, project_id: str, document_id: str) -> bool:
        document = self.store.project_document(document_id)
        if document is None or document["project_id"] != project_id:
            return False
        return self.store.delete_project_document(document_id, actor="Dashboard")

    def complete_task(self, project_id: str, document_id: str) -> dict | None:
        return self.store.complete_project_task(project_id, document_id, actor="Dashboard")

    def brief(self, project_id: str) -> dict:
        return self.store.project_brief(project_id)

    def brief_markdown(self, project_id: str) -> str:
        return self.store.project_brief_markdown(project_id)
