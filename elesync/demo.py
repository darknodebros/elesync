"""Scripted demo for `ele demo`.

A presenter drives the three demo moments from the keyboard instead of relying
on live AI apps: import an AI history (projects are suggested), Claude saves a
decision, ChatGPT searches it, ChatGPT contradicts it. Each assistant step goes
through the same MemoryService calls and audit attribution a real MCP client
uses, so the dashboard shows the genuine relay notifications and disagreement
card. It only ever runs against the throwaway vault `ele demo` creates.
"""

from __future__ import annotations

import contextvars

STEPS = [
    {"id": "import", "key": "1", "label": "Import AI history"},
    {"id": "connect", "key": "2", "label": "Give Claude and ChatGPT access"},
    {"id": "claude-saves", "key": "3", "label": "Claude saves the Kyoto hotel"},
    {"id": "chatgpt-searches", "key": "4", "label": "ChatGPT searches the trip"},
    {"id": "chatgpt-contradicts", "key": "5", "label": "ChatGPT books a different hotel"},
    {"id": "revoke-chatgpt", "key": "6", "label": "Revoke ChatGPT's access"},
    {"id": "chatgpt-blocked", "key": "7", "label": "ChatGPT tries again"},
    {"id": "connect-grok", "key": "8", "label": "Give Grok access"},
    {"id": "grok-catches-up", "key": "9", "label": "Grok catches up on the trip"},
    {"id": "phone-note", "key": "0", "label": "A note arrives from your phone"},
]

HISTORY = [
    (
        "Planning a Kyoto trip",
        "Help me plan five days in Kyoto in October.",
        "Day 1 Fushimi Inari, day 2 Arashiyama bamboo grove, day 3 Gion at dusk.",
    ),
    (
        "Kyoto trip budget",
        "What is a realistic budget for a Kyoto trip for two?",
        "Around 150 per day covers a mid-range hotel, food and trains.",
    ),
    (
        "Ryokan options in Kyoto",
        "Compare ryokan near Gion for our Kyoto trip.",
        "Three ryokan with private onsen and kaiseki dinners.",
    ),
    (
        "JR pass for Japan",
        "Is the JR pass worth it for Tokyo to Kyoto and back?",
        "For a single round trip it usually is not worth it.",
    ),
    (
        "Kyoto restaurants",
        "Best places to eat in Kyoto near our ryokan?",
        "Try Nishiki Market for lunch and a kaiseki dinner in Gion.",
    ),
    (
        "Helios launch plan",
        "Outline a launch plan for Helios, our note-taking app.",
        "Private beta, then a Product Hunt launch, then paid plans.",
    ),
    (
        "Helios pricing",
        "Should Helios pricing be monthly or annual?",
        "Offer both, with two months free on annual.",
    ),
    (
        "Helios landing page copy",
        "Write landing page copy for the Helios launch.",
        "Capture every idea. Find it instantly.",
    ),
    (
        "Helios beta onboarding",
        "Draft the onboarding email for Helios beta users.",
        "Welcome to the Helios beta, here is how to start.",
    ),
    (
        "Resume review",
        "Review my resume for a product manager role.",
        "Lead with outcomes and quantify impact.",
    ),
    (
        "Interview prep product manager",
        "Run a mock interview for a product manager role at a fintech.",
        "Tell me about a product you shipped end to end.",
    ),
    (
        "Cover letter",
        "Draft a cover letter for the product manager role at Stripe.",
        "Dear hiring team, I am excited to apply...",
    ),
    (
        "Sourdough starter",
        "My sourdough starter smells like acetone.",
        "It is hungry; feed it more often.",
    ),
    (
        "Houseplant care",
        "Why are my monstera leaves turning yellow?",
        "Usually overwatering; let the soil dry out.",
    ),
]

HOTEL_GION = "Hotel in Kyoto is booked near Gion for October 12-15."
HOTEL_STATION = "Hotel in Kyoto is booked near Kyoto Station for October 12-15."
PHONE_NOTE = "Pick up the JR pass at Kyoto Station when we land on October 12."


def history_export() -> list[dict]:
    """The demo history as a Claude conversations export."""
    chats = []
    for n, (title, human, assistant) in enumerate(HISTORY):
        stamp = f"2026-08-{n + 1:02d}T10:00:00Z"
        chats.append(
            {
                "uuid": f"00000000-0000-4000-8000-{n:012d}",
                "name": title,
                "summary": "",
                "created_at": stamp,
                "updated_at": stamp,
                "account": {"uuid": "elesync-demo"},
                "chat_messages": [
                    {
                        "uuid": f"demo-{n}-h",
                        "sender": "human",
                        "text": human,
                        "content": [{"type": "text", "text": human}],
                    },
                    {
                        "uuid": f"demo-{n}-a",
                        "sender": "assistant",
                        "text": assistant,
                        "content": [{"type": "text", "text": assistant}],
                    },
                ],
            }
        )
    return chats


def stable_project_id(name: str) -> str:
    """The id a demo project gets on every run, derived from its name."""
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, "elesync-demo:" + name.strip().lower()).hex


def _demo_project(store) -> dict:
    for project in store.projects():
        if "kyoto" in project["name"].lower():
            return project
    return store.create_project("Kyoto trip", "Planning October travel", actor="Demo")


def _as_assistant(ai: str, store, vault_dir, project: dict, action):
    """Run `action(service)` as assistant `ai` through the same project access
    check and audit attribution the MCP server applies to a real client.
    Returns the action's result, or the refusal when access is not granted."""
    from .access import connection_permissions
    from .audit import AuditLog, current_ai
    from .service import MemoryService

    def run():
        current_ai.set(ai)
        audit = AuditLog(vault_dir)
        try:
            permissions = connection_permissions(
                store, project["id"], ai, audit=audit, client_label="project connection"
            )
        except PermissionError as refused:
            return str(refused)
        service = MemoryService(
            store,
            audit=audit,
            project_id=project["id"],
            project_read=permissions["can_read"],
            project_write=permissions["can_write"],
            project_read_conversations=permissions["can_read_conversations"],
        )
        return action(service)

    return contextvars.copy_context().run(run)


def _grant(store, project: dict, provider: str) -> None:
    store.set_project_access(project["id"], provider, True, True, False, actor="Dashboard")


def run_step(store, vault_dir, step: str) -> dict:
    """Perform one server-side demo step (the import step runs in the browser).

    Returns the step, the project it acted on and, for owner actions such as
    granting or revoking access, a `notice` for the dashboard to show."""
    from .project_service import ProjectService

    project = _demo_project(store)
    notice = ""
    if step == "connect":
        _grant(store, project, "claude")
        _grant(store, project, "chatgpt")
        message = "granted"
        notice = f"Claude and ChatGPT can now read and update {project['name']}"
    elif step == "claude-saves":
        message = _as_assistant(
            "claude",
            store,
            vault_dir,
            project,
            lambda svc: svc.remember(HOTEL_GION, type="fact", source="claude"),
        )
    elif step == "chatgpt-searches":
        message = _as_assistant(
            "chatgpt",
            store,
            vault_dir,
            project,
            lambda svc: svc.recall_project(project["id"], "Kyoto hotel"),
        )
    elif step == "chatgpt-contradicts":
        message = _as_assistant(
            "chatgpt",
            store,
            vault_dir,
            project,
            lambda svc: svc.remember(HOTEL_STATION, type="fact", source="chatgpt"),
        )
    elif step == "revoke-chatgpt":
        ProjectService(store).revoke_access(project["id"], "chatgpt")
        message = "revoked"
        notice = f"ChatGPT no longer has access to {project['name']}"
    elif step == "chatgpt-blocked":
        message = _as_assistant(
            "chatgpt",
            store,
            vault_dir,
            project,
            lambda svc: svc.recall_project(project["id"], "Kyoto hotel"),
        )
    elif step == "connect-grok":
        _grant(store, project, "grok")
        message = "granted"
        notice = f"Grok can now read and update {project['name']}"
    elif step == "grok-catches-up":
        message = _as_assistant(
            "grok",
            store,
            vault_dir,
            project,
            lambda svc: svc.project_context(project["id"]),
        )
    elif step == "phone-note":
        from . import normalize
        from .audit import record_capture

        items = normalize.from_plaintext(PHONE_NOTE, source="EleSync companion", source_ref="phone")
        store.import_many(items, project_id=project["id"])
        record_capture(vault_dir, [item.id for item in items if store.get(item.id)])
        message = "captured"
    else:
        raise ValueError(f"unknown demo step: {step}")
    first_line = str(message).splitlines()[0] if str(message) else ""
    return {
        "step": step,
        "project": project["name"],
        "project_id": project["id"],
        "message": first_line,
        "notice": notice,
    }
