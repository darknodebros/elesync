---
id: mem_learn_decorators_002
type: fact
source: manual
tags: [python, patterns, learning]
created_at: 2026-06-28T14:15:00Z
---

# Python Decorators: Key Insights

Decorators are functions that take a function as input and return a modified function.

**Three-step mental model:**
1. Define wrapper function inside decorator
2. Wrapper calls original function with args/kwargs
3. Return wrapper (not calling it)

**Common use cases:**
- Timing/profiling (measure function execution)
- Caching (memoization)
- Authentication checks
- Logging before/after

**Gotcha:** `@decorator` is syntactic sugar for `func = decorator(func)`. The original function reference is lost unless you use `functools.wraps`.

**Example:**
```python
def timer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        print(f"Took {time.time() - start}s")
        return result
    return wrapper
```

Avoid: writing decorators that assume specific function signatures.
