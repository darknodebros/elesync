"""Lock the MCP contract that configured AI clients depend on.

Tool names, parameter names, JSON types, defaults and required-ness are what a
client (and the model driving it) learned. Changing any of them silently breaks
users who upgrade. If a change here is intentional, update CONTRACT in the same
commit and call it out in the CHANGELOG — prefer keeping the old name working.
"""

import asyncio
import tempfile
import unittest

REQUIRED = object()

# tool -> {param: (json type, default or REQUIRED)}
CONTRACT = {
    "recall": {
        "query": ("string", REQUIRED),
        "limit": ("integer", 8),
        "source": ("string", ""),
        "type": ("string", ""),
        "semantic_weight": ("number", -1.0),
    },
    "list_projects": {},
    "recall_project": {
        "project_id": ("string", REQUIRED),
        "query": ("string", REQUIRED),
        "limit": ("integer", 8),
    },
    "project_context": {
        "project_id": ("string", REQUIRED),
        "query": ("string", ""),
        "document_kind": ("string", ""),
        "offset": ("integer", 0),
        "limit": ("integer", 20),
        "max_chars": ("integer", 100_000),
    },
    "save_project_document": {
        "project_id": ("string", REQUIRED),
        "title": ("string", REQUIRED),
        "body": ("string", REQUIRED),
        "kind": ("string", "knowledge"),
    },
    "remember": {
        "content": ("string", REQUIRED),
        "type": ("string", "note"),
        "tags": ("string", ""),
        "source": ("string", "ai"),
        "scopes": ("string", ""),
        "supersedes": ("string", ""),
        "template": ("string", ""),
        "fields": ("string", ""),
    },
    "forget": {"memory_id": ("string", REQUIRED)},
    "memory_status": {},
    "list_scopes": {},
    "find_conflicts": {},
    "history": {"memory_id": ("string", REQUIRED)},
    "link_memories": {
        "source_id": ("string", REQUIRED),
        "target_id": ("string", REQUIRED),
        "relation": ("string", "related"),
        "remove": ("boolean", False),
    },
    "related_memories": {"memory_id": ("string", REQUIRED)},
    "audit_log": {
        "last": ("integer", 50),
        "operation": ("string", ""),
        "client": ("string", ""),
    },
}

RESOURCES = {"memory://recent", "memory://templates"}


class TestMCPContract(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from mcp.server.fastmcp import FastMCP  # noqa: F401
        except ImportError:
            self.skipTest("MCP SDK not installed")
        from elesync.mcp_server import build_server

        self.tmp = tempfile.TemporaryDirectory()
        self.server = build_server(self.tmp.name)
        self.store = self.server.elesync_store

    def tearDown(self) -> None:
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_tool_names_are_locked(self) -> None:
        tools = asyncio.run(self.server.list_tools())
        self.assertEqual(sorted(t.name for t in tools), sorted(CONTRACT))

    def test_tool_parameters_are_locked(self) -> None:
        tools = {t.name: t for t in asyncio.run(self.server.list_tools())}
        for name, params in CONTRACT.items():
            with self.subTest(tool=name):
                schema = tools[name].inputSchema
                props = schema.get("properties", {})
                self.assertEqual(sorted(props), sorted(params), "parameter names changed")
                required = {p for p, (_, default) in params.items() if default is REQUIRED}
                self.assertEqual(set(schema.get("required", [])), required, "required set changed")
                for param, (json_type, default) in params.items():
                    with self.subTest(param=param):
                        self.assertEqual(props[param].get("type"), json_type)
                        if default is not REQUIRED:
                            self.assertEqual(props[param].get("default"), default)

    def test_tools_have_descriptions(self) -> None:
        # The description is what the model reads to decide when to call a tool.
        for tool in asyncio.run(self.server.list_tools()):
            with self.subTest(tool=tool.name):
                self.assertGreaterEqual(len((tool.description or "").strip()), 30)

    def test_resources_are_locked(self) -> None:
        resources = asyncio.run(self.server.list_resources())
        self.assertEqual({str(r.uri) for r in resources}, RESOURCES)


if __name__ == "__main__":
    unittest.main()
