---
id: mem_conv_code_review_005
type: conversation
source: claude
tags: [code-review, debugging, async]
created_at: 2026-06-15T16:20:00Z
---

# Code Review: Async Event Handler

user: My event handler sometimes misses events. It's async and uses a queue. What could be wrong?

assistant: A few common causes:

1. **Queue filling up**: If events come faster than they're processed, the queue has a max size. Add `.put_nowait()` error handling or increase queue size.

2. **Task not awaited**: If you're not `await`-ing the handler coroutine, it starts but doesn't complete before the next event fires.

3. **Exception swallowing**: Wrap your async function in try/except to surface errors. Silent failures = "missed" events.

4. **Race condition**: If multiple handlers write to the same resource without locks, events might overwrite each other.

What does your handler look like? Post the code and I can pinpoint it.

user: Here's the handler. I think the queue is fine...

```python
asyncio.create_task(process_event(event))
```

assistant: Found it. You're creating the task but not awaiting. If the process shuts down or the event loop cycles, the coroutine never runs.

Fix: Use a background queue + a persistent consumer task:

```python
queue = asyncio.Queue()

async def consumer():
    while True:
        event = await queue.get()
        await process_event(event)
        queue.task_done()

async def on_event(event):
    await queue.put(event)

# Start consumer on app init
asyncio.create_task(consumer())
```

This ensures events are processed even if the handler returns early.
