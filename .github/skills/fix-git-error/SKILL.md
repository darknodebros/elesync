---
title: "Fix Repository / Git Errors"
related: ["agent-customization"]
description: |
  A concise, reproducible workflow for diagnosing and resolving common repository and Git errors. Use this skill when a developer reports a Git failure (merge/rebase conflicts, push/pull errors, permission issues, corrupted refs, LFS/submodule problems) or when CI fails with repository-related errors.
when_to_use: |
  - Any time `git` returns an error the developer can't resolve quickly.
  - When a failed push/pull/rebase/merge blocks development or CI.
  - When repository state appears inconsistent (missing tags, corrupted refs, unexpected detached HEAD).
outcome: |
  A reproducible set of diagnostic steps, a chosen remediation path (safe or aggressive), and verification checks that confirm the repository is healthy and changes can be pushed.
-
workflow:
  - name: Gather context
    steps:
      - Ask for the exact error message and the command that produced it.
      - Ask for the current branch: `git branch --show-current` and status: `git status --porcelain --branch`.
      - Ask whether other collaborators see the same issue and whether CI is failing.

  - name: Local quick checks
    steps:
      - Run `git status` and `git rev-parse --abbrev-ref HEAD`.
      - Inspect last commits: `git log --oneline -n 10`.
      - Check remotes: `git remote -v` and `git ls-remote --heads origin`.

  - name: Decide branch safety
    decision_points:
      - condition: "Working tree clean and no local-only commits"
        action: "Prefer safe remote-sync commands (fetch, pull)"
      - condition: "Uncommitted work present"
        action: "Stash or commit before aggressive operations"

  - name: Common remediation paths
    cases:
      - title: "Network / auth errors"
        steps:
          - Check `git remote -v` and try `git fetch --all`.
          - Validate credentials: ask to try `git fetch` with `GIT_TRACE=1` if needed.
          - If using SSH, confirm `ssh -T git@github.com` works.

      - title: "Merge / rebase conflicts"
        steps:
          - Run `git status` to see conflicting files.
          - Use `git mergetool` or manual resolution, then `git add` + `git rebase --continue` or `git commit`.
          - If rebase is too complex, suggest `git rebase --abort` and use a merge instead.

      - title: "Corrupt refs / missing objects"
        steps:
          - Run `git fsck --full` to surface corruption.
          - Try `git reflog` to find a recoverable commit.
          - If necessary, fetch a fresh copy: clone the repo into a temporary directory and compare.

      - title: "Large File / LFS problems"
        steps:
          - Verify LFS is installed: `git lfs version`.
          - Run `GIT_TRANSFER_TRACE=1 git push` for diagnostics.

  - name: Aggressive recovery (when safe)
    steps:
      - Create a backup: `git bundle create ../repo-backup.bundle --all` or `git clone --mirror . ../repo-mirror`
      - If local history is irrecoverable, reclone: `git clone <url> tmp && mv tmp/.git . && git reset --hard origin/<branch>`

quality_criteria:
  - The repository reports a clean working tree: `git status` shows nothing to commit.
  - The branch can fetch and push successfully: `git fetch --all` and `git push` return success.
  - CI or downstream checks that failed because of the repo error are re-run and pass (if applicable).

verification:
  - Confirm `git status --porcelain` is empty.
  - Confirm `git rev-parse origin/$(git rev-parse --abbrev-ref HEAD)` succeeds.
  - If changes were rewritten (rebase/force-push), ensure collaborators are informed and given instructions to synchronize (e.g., `git fetch && git reset --hard origin/<branch>`).

examples_and_prompts:
  - Example user prompt: "I'm getting 'error: failed to push some refs' after a merge — please help."
  - Agent prompt to run: "Collect `git status --porcelain --branch`, `git log -n 5 --oneline`, and the exact failing command and error output. Then run `git fetch --all` and report results."

notes:
  - Prefer conservative fixes first (stash/commit, fetch, merge) before destructive actions.
  - Always create a backup bundle or mirror before attempting destructive recovery.
  - For workspace-scoped customization, add repository-specific checks (e.g., submodules, LFS, CI hooks) to the `workflow` section.

project_specific_checks:
  description: |
    EleSync-specific checks and commands to run when diagnosing repository or release-related errors.
  checks:
    - name: CI matrix and test command
      command: |
        - CI runs on Python 3.10–3.14. Run tests locally the same way:
          `python -m unittest discover -s tests -v`

    - name: Install extras for optional tests
      command: |
        - The project defines optional extras in `pyproject.toml` (e.g. `mcp`, `encryption`, `watch`, `webauth`, `semantic`).
        - To replicate CI's `test-extras` job: `pip install -e ".[dev]"` then run the test command above.

    - name: Linting and static checks
      command: |
        - CI runs `ruff` and `mypy`. Reproduce with:
          `pip install -e ".[dev]" ruff mypy`
          `ruff check elesync/ tests/` then `mypy elesync/`.

    - name: Release and packaging
      command: |
        - Releases build executables via `scripts/build.py` and use `pyinstaller`.
        - Releases stay private on GitHub. Nothing is published to PyPI, the MCP Registry, Scoop or other public registries (see RELEASING.md).


next_steps:
  - Save this file as `.github/skills/fix-git-error/SKILL.md` in the repository.
  - Ask the reporter for the exact error output and the commands they ran.
  - If more repository-specific instructions are needed, iterate on the `workflow` with concrete project hooks and CI steps.
