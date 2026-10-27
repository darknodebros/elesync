---
id: mem_pref_code_style_003
type: preference
source: manual
tags: [coding, style, conventions]
created_at: 2026-06-25T09:00:00Z
---

# My Coding Style Preferences

**Naming:**
- Functions/variables: snake_case
- Classes: PascalCase
- Constants: UPPER_SNAKE_CASE
- Private methods: _leading_underscore

**Function length:**
- Target: single responsibility, <30 lines
- If >50 lines, consider splitting
- Exception: data parsing/transformation can be longer

**Comments:**
- Only "why", never "what" (code shows what)
- Exception: non-obvious algorithms or performance hacks
- Header comment on complex functions

**Type hints:**
- Use type hints in all new code (Python 3.9+)
- Avoid `typing.Any` unless truly needed
- Be specific: `list[str]` not `list`

**Error handling:**
- Catch specific exceptions, not bare `except:`
- Let exceptions propagate if you can't handle them meaningfully
- Use context managers for resource cleanup

**Testing:**
- Aim for 80%+ coverage
- Test edge cases, not happy path only
- Integration tests > unit tests when trade-off needed
