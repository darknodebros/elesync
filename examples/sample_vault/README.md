# Sample Vault

Example EleSync vault for testing, demos, and onboarding new users.

## What's included

This vault contains 5 sample memories across different types:

1. **Project Idea** (`project_idea_task_planner.md`) — Note type
   - Brainstorm for a task planning application
   - Demonstrates idea capture + reasoning

2. **Python Decorators** (`learning_python_decorators.md`) — Fact type
   - Technical learning from books or tutorials
   - Key insights + code examples

3. **Code Style Preferences** (`preference_code_style.md`) — Preference type
   - Personal coding conventions
   - Shared across projects for consistency

4. **Dev Setup Guide** (`instruction_local_dev_setup.md`) — Instruction type
   - Step-by-step procedures
   - Environment variables, common issues, workflows

5. **Code Review Chat** (`conversation_ai_code_review.md`) — Conversation type
   - Multi-turn dialogue with an AI assistant
   - Shows problem-solving and debugging patterns

## How to use

### Option 1: Import into existing vault

```bash
# Copy sample files into your vault
cp examples/sample_vault/notes/*.md ~/EleSyncVault/notes/

# Reindex to rebuild search index
ele reindex
```

### Option 2: Use as a starting point

```bash
# Create a new vault with samples
cp -r examples/sample_vault ~/MyVault
ele search --vault ~/MyVault "Python"
ele web --vault ~/MyVault
```

### Option 3: Test the web UI

```bash
ele web --vault examples/sample_vault
# Opens web UI with sample memories loaded
# Try: search, browse, view details, export to JSON
```

## Exploring the vault

**Via CLI:**
```bash
ele search "decorator" --vault examples/sample_vault
ele search "Python" --vault examples/sample_vault
```

**Via web UI:**
- Click Browse to see all memories
- Use Search tab to find specific topics
- Click a memory card to view full details
- Export to JSON to see structured format

## Memory structure

Each file is a valid Markdown with YAML front-matter:

```yaml
---
id: unique_memory_id
type: note|fact|preference|instruction|conversation
source: manual|chatgpt|claude|gemini|...
tags: [tag1, tag2]
created_at: ISO8601 timestamp
---

# Content

Markdown content here.
```

The `id` field must be globally unique. Tags help organize memories across scopes.

## What to try

1. **Search**: Find memories by keyword (e.g., "Python", "setup")
2. **Export**: Download as JSON to inspect the data structure
3. **Add your own**: Create a new `.md` file with same structure
4. **Reindex**: Run `ele reindex` to rebuild the search index after adding files
5. **Web UI**: Browse and search visually in the dashboard

## Notes

- This vault is read-only in the repo (no CI modifications)
- Copy it to your local directory before making changes
- Entries use realistic examples but are not necessarily correct — treat as illustrations
- See main README for how to create your own memories
