# How the addon talks to the API: worker thread or subprocess?

Type: grilling
Status: open
Blocked by: none

## Question

Transport was assumed settled while charting, and the research says it is not: the thread-plus-queue recipe works, but the docs do not sanction it and no bundled addon uses it — while Blender's own downloads use a subprocess and a `Pipe`. Decide which one, and what follows from the choice.

1. **Thread + queue (undocumented).** Accept that the docs forbid using `bpy` while threads run, and that their example of *unsupported* usage is precisely a thread-driven repeating timer? What is the real risk — GIL/RNA races, a crash during file load, shutdown hangs — and is it contained by strictly confining the worker to plain-Python I/O and a queue of non-RNA values? Check whether this is treated as safe in practice by addon authors.
2. **Subprocess + `Pipe` (sanctioned).** What process would it be, given Blender's Python is frozen and no external `python3` is guaranteed? How would the worker be launched, kept alive, supervised, shut down, and would it survive loading a new `.blend`? Every chunk then crosses a process boundary — how, without wrecking latency?
3. **What the choice changes elsewhere.** Does a subprocess alter the extension's network-permission or packaging story? Does it change how Stop/cancellation works? Does it change the panel prototype's scope?
4. **The fallback.** Whatever wins, what happens when the transport cannot start at all — fail visibly, or degrade to non-streaming?

Read *How do we call the API off-thread and stream the reply into the UI?* first; it owns the facts.

Deliverable: the decision, the argument against the option you rejected, and the lifecycle rules — start, stop, restart, crash.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
