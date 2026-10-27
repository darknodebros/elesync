"""Typed JSON shapes shared by the local web API and its clients.

These are static contracts only. They do not introduce a validation dependency
or change the wire format of the existing API.
"""

from __future__ import annotations

from typing import TypedDict

MemoryJson = dict[str, object]
DocumentJson = dict[str, object]


class ProjectSummary(TypedDict, total=False):
    id: str
    name: str
    description: str
    status: str
    memory_count: int
    document_count: int
    created_at: str
    updated_at: str


class ProjectDetail(ProjectSummary, total=False):
    memories: list[MemoryJson]
    documents: list[DocumentJson]


class ProjectListResponse(TypedDict):
    projects: list[ProjectSummary]


class ProjectAccess(TypedDict, total=False):
    project_id: str
    provider: str
    can_read: bool | int
    can_write: bool | int
    can_read_conversations: bool | int
    status: str
    created_at: str
    updated_at: str


class ProjectAccessResponse(TypedDict):
    access: list[ProjectAccess]


class ProjectActivityEvent(TypedDict):
    type: str
    title: str
    detail: str
    actor: str
    at: str


class ProjectActivityResponse(TypedDict):
    events: list[ProjectActivityEvent]


class ProjectSearchResponse(TypedDict):
    items: list[MemoryJson]


class ProjectExportResponse(TypedDict):
    filename: str
    markdown: str


class ApiErrorResponse(TypedDict):
    error: str
