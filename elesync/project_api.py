"""HTTP-facing project API operations.

The web server owns transport and status-code handling.  This module owns the
JSON-ready project response shapes and delegates business rules to
``ProjectService``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast

from .api_models import (
    ProjectAccessResponse,
    ProjectActivityResponse,
    ProjectDetail,
    ProjectExportResponse,
    ProjectListResponse,
    ProjectSearchResponse,
)
from .project_service import ProjectService


class ProjectApi:
    def __init__(self, service: ProjectService, item_to_dict: Callable[[object], dict]):
        self.service = service
        self.item_to_dict = item_to_dict

    def list(self) -> ProjectListResponse:
        return cast(ProjectListResponse, {"projects": self.service.list_projects()})

    def get(self, project_id: str) -> ProjectDetail | None:
        project = self.service.get(project_id)
        if project is None:
            return None
        project["memories"] = [self.item_to_dict(item) for item in project["memories"]]
        return cast(ProjectDetail, project)

    def create(self, body: dict) -> dict:
        return self.service.create(body.get("name", ""), body.get("description", ""))

    def discover(self, params: dict) -> dict:
        try:
            limit = int(params.get("limit", ["5"])[0])
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
        return self.service.discover(limit)

    def accept_discovered(self, body: dict) -> dict:
        projects = body.get("projects") if isinstance(body, dict) else None
        if not isinstance(projects, list) or not projects:
            raise ValueError("projects must be a non-empty list")
        if len(projects) > 10:
            raise ValueError("create at most 10 projects at a time")
        plans = []
        for entry in projects:
            if not isinstance(entry, dict):
                raise ValueError("each project must be an object")
            name = str(entry.get("name") or "").strip()
            memory_ids = entry.get("memory_ids") or []
            if not name:
                raise ValueError("every project needs a name")
            if not isinstance(memory_ids, list) or len(memory_ids) > 5000:
                raise ValueError("memory_ids must be a list of at most 5000 ids")
            plans.append((name, str(entry.get("description") or ""), memory_ids))
        created = [
            self.service.create_from_suggestion(name, description, memory_ids)
            for name, description, memory_ids in plans
        ]
        return {"created": created}

    def add_memory(self, project_id: str, body: dict) -> dict:
        return {"added": self.service.add_memory(project_id, body.get("memory_id", ""))}

    def update(self, project_id: str, body: dict) -> dict | None:
        current = self.service.get(project_id)
        if current is None:
            return None
        return self.service.update(
            project_id,
            body.get("name", current["name"]),
            body.get("description", current["description"]),
            body.get("status", current.get("status", "active")),
        )

    def suggestions(self, project_id: str) -> dict:
        return {
            "semantic_enabled": bool(self.service.store.embedder),
            "suggestions": [
                {
                    "memory": self.item_to_dict(row["item"]),
                    "matched_terms": row["matched_terms"],
                    "similarity": row["similarity"],
                    "match_kind": row["match_kind"],
                }
                for row in self.service.suggestions(project_id)
            ],
        }

    def search(self, project_id: str, params: dict) -> ProjectSearchResponse:
        query = params.get("q", [""])[0]
        return cast(
            ProjectSearchResponse,
            {"items": [self.item_to_dict(item) for item in self.service.search(project_id, query)]},
        )

    def context(self, project_id: str) -> dict:
        return self.service.context(project_id)

    def timeline(self, project_id: str) -> ProjectActivityResponse:
        return cast(ProjectActivityResponse, {"events": self.service.timeline(project_id)})

    def access(self, project_id: str) -> ProjectAccessResponse:
        return cast(ProjectAccessResponse, {"access": self.service.access(project_id)})

    def set_access(self, project_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return self.service.set_access(
            project_id,
            body.get("provider", ""),
            bool(body.get("can_read", True)),
            bool(body.get("can_write", False)),
            bool(body.get("can_read_conversations", False)),
        )

    def create_document(self, project_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return self.service.save_document(
            project_id,
            body.get("title", ""),
            body.get("body", ""),
            body.get("kind", "knowledge"),
        )

    def update_document(self, project_id: str, document_id: str, body: dict) -> dict | None:
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        document = self.service.store.project_document(document_id)
        if document is None or document["project_id"] != project_id:
            return None
        return self.service.update_document(
            project_id,
            document_id,
            body.get("title", document["title"]),
            body.get("body", document["body"]),
            body.get("kind", document["kind"]),
        )

    def delete_document(self, project_id: str, document_id: str) -> bool:
        return self.service.delete_document(project_id, document_id)

    def complete_task(self, project_id: str, document_id: str) -> dict | None:
        return self.service.complete_task(project_id, document_id)

    def brief(self, project_id: str) -> dict:
        return self.service.brief(project_id)

    def handoff(self, project_id: str) -> dict:
        prompt = self.service.handoff(project_id)
        return {"prompt": prompt, "characters": len(prompt)}

    def export(self, project_id: str) -> ProjectExportResponse:
        project = self.service.get(project_id)
        if project is None:
            raise ValueError("project not found")
        safe_name = "-".join(re.findall(r"[\w]+", project["name"].lower()))
        return cast(
            ProjectExportResponse,
            {
                "filename": f"{safe_name or 'project'}-brief.md",
                "markdown": self.service.brief_markdown(project_id),
            },
        )
