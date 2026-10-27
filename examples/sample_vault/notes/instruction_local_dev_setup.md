---
id: mem_instr_dev_setup_004
type: instruction
source: manual
tags: [setup, development, automation]
created_at: 2026-06-20T11:45:00Z
---

# Local Development Setup

**Prerequisites:**
- Python 3.10+
- Git
- Optional: Docker for external services

**Quick start:**

```bash
git clone https://github.com/darknodebros/EleSync
cd EleSync
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -e ".[dev]"
pre-commit install
```

**Run tests:**
```bash
pytest tests/ -xvs
```

**Start web UI:**
```bash
ele web --port 7477
```

**Common issues:**

1. Pre-commit hook fails: Run `ruff format elesync/` and `mypy elesync/`
2. Tests fail on Windows: Use PowerShell, not cmd.exe
3. Web UI won't start: Check port 7477 isn't already in use

**Git workflow:**
- Branch from `master`
- Commit message: `feat:`, `fix:`, `docs:`, `chore:`
- Pre-commit hook runs ruff, mypy, trailing whitespace
- Don't force-push unless discussed

**Environment variables:**
- `ELESYNC_DIR`: vault path (default: ~/EleSyncVault)
- `ELESYNC_PASSPHRASE`: for encrypted vaults
- `ELESYNC_SEMANTIC`: enable semantic search
