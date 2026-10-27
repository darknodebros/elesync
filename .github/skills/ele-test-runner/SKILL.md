++
Name: ele-test-runner

Description:
A small, focused skill that documents how to run tests, linters, and common verifier commands for EleSync. Use this to quickly orient agents (or humans) on the minimal commands and expectations for validating changes.

Purpose:
- Give concise, copy-paste commands to run tests and linters.
- Explain when and how to run targeted tests.
- Provide quick troubleshooting steps for common failures.

Where it lives:
- This file: `.github/skills/ele-test-runner/SKILL.md`

Commands (copy-paste):
- Dev install: `pip install -e .[dev]` or `pip install ".[dev]"`
- Run full test suite: `pytest`
- Run a single test file: `pytest tests/test_module.py`
- Run a single test function: `pytest tests/test_module.py::test_function`
- Run linters/formatters: `ruff check .` and `ruff format .`

When to run:
- Before opening a PR: run `pytest` (or targeted tests for your changes) and `ruff check .`.
- For CI-like quick checks locally: run `pytest -q` and `ruff check .`.

Expectations / outputs:
- Passing tests: exit code 0 and summary `== X passed`.
- Failing tests: pytest shows failing trace and assertion — add or update tests and fix code or tests as appropriate.
- Linting: `ruff check` lists issues; run `ruff format .` to fix formatting, then re-run `ruff check`.

Troubleshooting:
- If tests fail due to missing optional dependencies, re-run with dev extras: `pip install ".[dev]"`.
- For environment-specific errors, run the failing test individually to get a focused traceback.
- If a test appears flaky, mark with `@pytest.mark.flaky` or open an issue and add a regression test.

Notes for agents:
- Prefer minimal, well-tested changes. Add a test for bug fixes or behavior changes.
- Do not modify CI configuration in this skill; link to `pyproject.toml` for tooling details.
