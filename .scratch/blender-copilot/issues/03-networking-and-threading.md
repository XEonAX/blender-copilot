# How do we call the API off-thread and stream the reply into the UI?

Type: research
Status: resolved
Blocked by: none

## Question

How does the addon talk to an OpenAI-compatible API and show a reply appearing incrementally, without freezing Blender — given that `bpy` is main-thread-only and `bpy.app.timers` only pump from the GUI event loop?

Answer at least:

1. **The off-thread pattern.** What is the supported way for a worker thread to do blocking HTTP and hand results back to the main thread? Confirm the queue-drained-by-a-timer recipe and what is *illegal* off-thread (`bpy` access, `bpy.context`, RNA writes, scene reads). Cite the threading gotchas doc and any bundled addon that does this.
2. **Streaming with `requests`.** Can the bundled `requests` do SSE (`stream=True` + `iter_lines()`), and how do we parse OpenAI-style `data:` chunks — including **tool-call deltas**, which arrive as fragmented `function.arguments` strings that must be accumulated? Report the exact accumulation rules.
3. **Repainting.** How does the main thread turn arrived chunks into visible text? What must the timer callback do to trigger a panel redraw, and how often can it safely run?
4. **Cancellation.** How do we abort an in-flight stream when the user hits Stop? Can a worker thread be reliably interrupted mid-read (`response.close()`, a cancellation flag, timeouts), and what happens to a stream that is abandoned?
5. **Failure surfaces.** What an auth failure, rate limit, malformed tool-call JSON, and a dropped connection each look like, and how each should map to a message in the panel. Are these distinguishable from `requests` alone?
6. **Tool-call loop timing.** The loop is: request → model asks for a tool → we execute in Blender on the main thread → send results back → request again. Where do the thread boundaries fall, and is there a deadlock risk if the worker holds a lock while the main thread needs to execute `bpy`?
7. **What cannot be verified without an API key**, stated explicitly. TLS reachability to `api.openai.com` is already verified; end-to-end streaming is not.

Related: *What exactly happens when we exec model code, and what does undo cover?* owns execution and undo; this ticket owns transport and threading.

## Answer

**The working recipe:** all `bpy` stays on the main thread; a worker does every HTTP/SSE byte and touches no `bpy` at all; the *only* shared object is a `queue.Queue` of plain Python values (never RNA objects), drained non-blockingly by a `bpy.app.timers` callback, which appends to Python-side state, calls `region.tag_redraw()` only when something changed, and returns the next interval. Tool calls invert it: the main thread executes `bpy` while the worker waits on a second queue. The main thread must never `join()` or block.

- **SSE with bundled `requests`:** `post(..., stream=True, timeout=(connect, read))`, `raise_for_status()` *before* iterating, then `iter_lines()`. Accumulation rules: concatenate `content`; index `tool_calls` by `index`; set `id`/`type`/`name` once; **concatenate** `function.arguments`, which arrives fragmented across chunks; stop at `[DONE]`.
- **Cancellation is the sharp edge:** `response.close()` does **not** unblock a reader (measured 30 s vs 1.5 s); only `response.raw.shutdown()` unblocks instantly, and that is urllib3 behaviour undocumented in `requests`. So Stop needs a cancellation flag plus bounded read timeouts rather than relying on close().
- **The pattern is not sanctioned.** The docs forbid using `bpy` while threads run, and their example of *unsupported* usage is precisely a thread-driven repeating timer. Blender's own equivalent ships a **subprocess** with a `Pipe` (`_bpy_internal/http/downloader.py`, and `bl_pkg` likewise uses subprocesses); no bundled addon uses threads + timers. This is why transport is now its own ticket rather than a settled decision.
- **Not verified:** real chunk shapes from a live endpoint, 429/`Retry-After` handling, and every GUI repaint timing measurement (timers never fire under `blender -b`). TLS reachability to `api.openai.com` was verified; end-to-end streaming was not.

Detail and citations: `research/blender-networking-threading.md`. Consequence: new ticket *How the addon talks to the API: worker thread or subprocess?*

## Comments
