"""Bounded, read-only graph of live memories, their tags, projects and direct links."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import split_link

if TYPE_CHECKING:
    from .store import MemoryStore

# A hard ceiling keeps the response and the canvas responsive on very large vaults.
GRAPH_MAX_MEMORIES = 20_000


def memory_graph(store: MemoryStore, project_id: str = "", limit: int = GRAPH_MAX_MEMORIES) -> dict:
    """Memories are stars, shared tags and projects are the hubs that connect them."""
    if project_id and not store.project(project_id):
        raise ValueError("Project not found")
    items, total = store.graph_memories(project_id, limit)
    shown_ids = {item["id"] for item in items}
    nodes: list[dict] = []
    edges: list[dict] = []
    members_by_tag: dict[str, list[str]] = {}
    members_by_project: dict[str, list[str]] = {}
    for project, memory_id in store.project_memberships():
        if memory_id in shown_ids:
            members_by_project.setdefault(project, []).append(memory_id)
    projects_of: dict[str, list[str]] = {}
    for project, members in members_by_project.items():
        for memory_id in members:
            projects_of.setdefault(memory_id, []).append(project)
    for item in items:
        links = item.pop("links", [])
        nodes.append({**item, "kind": "memory", "project_ids": projects_of.get(item["id"], [])})
        for tag in item["tags"]:
            members_by_tag.setdefault(tag, []).append(item["id"])
        # A link is only drawn when both ends are in view, so a filtered graph
        # never reveals a memory outside it.
        for link in links:
            relation, target = split_link(link)
            if target in shown_ids:
                edges.append(
                    {"source": item["id"], "target": target, "kind": "link", "relation": relation}
                )
    names = {p["id"]: p["name"] for p in store.projects() if p["id"] in members_by_project}
    for project, members in members_by_project.items():
        node_id = "project:" + project
        nodes.append({"id": node_id, "kind": "project", "label": names.get(project, "Project")})
        edges.extend({"source": m, "target": node_id, "kind": "project"} for m in members)
    # Single-use tags do not connect memories, so only shared tags become hubs.
    shared = sorted(
        (tag for tag, members in members_by_tag.items() if len(members) > 1),
        key=lambda tag: (-len(members_by_tag[tag]), tag),
    )
    for tag in shared:
        node_id = "tag:" + tag
        nodes.append({"id": node_id, "kind": "tag", "label": tag})
        edges.extend({"source": m, "target": node_id, "kind": "tag"} for m in members_by_tag[tag])
    return {
        "nodes": nodes,
        "edges": edges,
        "total": total,
        "shown": len(items),
        "truncated": total > len(items),
        "project_id": project_id,
    }
