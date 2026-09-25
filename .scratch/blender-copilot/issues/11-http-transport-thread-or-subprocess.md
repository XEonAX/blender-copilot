# How the addon talks to the API: worker thread or subprocess?

Type: grilling
Status: resolved
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

**PROVISIONAL — no human present. Recommendation: subprocess. The child is Blender's own
bundled `python3.13`, launched as `sys.executable <pkg>/_worker.py`, speaking
newline-delimited JSON over stdin/stdout. Reject the worker thread.**

### Why subprocess, and which subprocess

Measured on the installed 5.2.2 by running it in background mode (probes in `/tmp`, since
deleted):

- `sys.executable` inside Blender is **not** the Blender binary: it is
  `/Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13`, and
  `sys._base_executable`, `multiprocessing.spawn.get_executable()` and the spawned child's
  `sys.executable` are the same path. So "no external `python3` is guaranteed" answers
  itself — we ship one, and Blender tells us where it is.
- A `Popen` child of that interpreter sees the bundled site-packages: `requests 2.32.3` and
  `certifi 2025.04.26` import; `import bpy` raises `ModuleNotFoundError`. The isolation we
  want comes for free.
- Launch→`ready`: **0.021 s**. 50 JSON round-trips: **0.0012 s total (~0.02 ms each)**.
  `kill -9` is observed as `returncode == -9` plus stdout EOF; a replacement child starts
  cleanly; an explicit shutdown exits 0. The IPC tax is two orders of magnitude below one
  timer tick.

The process boundary is what the ticket asked; the *launch flavor* is a sub-choice resolved
by evidence. Blender core uses `multiprocessing.get_context("spawn")` + `Pipe`
(`_bpy_internal/http/downloader.py:417,661-675`), but its target must be importable **by
module name in the child**, and our code is not: the child has no `bl_ext` (that is a fake
package Blender builds at runtime in the parent — `addon_utils.py:1443+`), and even with the
extension dir on `sys.path`, importing any submodule executes `blender_copilot/__init__.py`,
which imports `bpy`. Spawn also needs Blender's private `_cleanup_main_file_attribute()`
hack for `__main__.__file__ == "<blender string>"`. `Popen([sys.executable, worker_path])`
avoids all three — the child is a bare script, no package import, no spawn bootstrap. That is
exactly `bl_pkg` (`bl_pkg/bl_extension_utils.py:226-252`). "Pipe" here means an OS pipe
(stdin/stdout), not `multiprocessing.Pipe`.

Worker rules that fall out: the worker file must be standalone, must never import `bpy` or
the extension package, and must log to stderr (stdout is the protocol).

### Why not the thread

- The docs do not merely omit it; they name our exact pattern — a daemon/repeating timer
  plus a live thread — as the **unsupported** example, predicting "random crashes or errors
  in Blender's own drawing code" (`info_gotchas_threading.rst:74-91`). The damage lands in
  the host we cannot patch.
- No precedent to copy: the installed-addon grep in
  `research/blender-networking-threading.md` §1 finds only FBX (inside a blocking op,
  explicitly "`bpy` cannot be imported here") and `ui_translate` (a **process** pool). No
  bundled addon pushes thread results into a timer queue, and every shipped
  background-network path — core downloader, `bl_pkg` — uses a process.
- The thread's only advantages are shared memory and no serialization; both are worthless
  here because shared state must already be plain values, and IPC costs 0.02 ms.
- The thread's costs are real: a crash in `requests`/urllib3/JSON takes Blender down with
  it; pure-Python parsing holds the GIL and can delay the UI timer; shutdown hangs are ours
  to debug.
- The counter-argument is that the crash rate is unmeasured (§7.7 calls it "the single
  largest unverified risk"). I take the documented verdict plus shipped precedent as
  decisive rather than field-testing a crash class on the user's GUI.

### Lifecycle rules (start, stop, restart, crash)

- **Start** — lazy, once per session, on the first turn. The main thread does only `Popen`
  (non-blocking, ~ms); it **never waits for `ready`**. The child emits `{"ev":"ready"}` and
  the drain timer surfaces it; a missing `ready` within ~5 s marks transport unavailable.
  Register the drain with
  `bpy.app.timers.register(drain, first_interval=0.05, persistent=True)` — `persistent=True`
  is what carries it across a `.blend` load.
- **Stop** — two levels. The main thread sets a flag and writes `{"cmd":"cancel"}`; the
  child checks it between chunks and bounded read timeouts (`timeout=(5, 1.0)`) cap the worst
  case at ~1 s. Inside the child, stdin is read by a helper thread that sets the
  cancel/shutdown event, and `response.raw.shutdown()` may be called from a thread *other
  than the reader* for an immediate unblock (ticket 3's measured result). Keep already
  streamed text, mark it "stopped". If the child ignores the cancel past a ~2 s grace,
  `proc.kill()` — a hard stop the thread option cannot offer. **The main thread never
  `join()`s and never blocks: the drain reads stdout only when `poll()` says data is ready
  (or a parent-side reader thread does).**
- **Restart** — never automatic, never in a loop. On EOF/exit, mark the in-flight turn
  failed and spawn a fresh child lazily on the next send.
- **Crash** — detect via `proc.poll() is not None` / stdout EOF; report "worker crashed
  (exit N)" with the last ~8 KB of stderr; keep partial text marked incomplete.
- **File load** — nothing to do: the child is independent of `bpy.data` and the persistent
  timer survives. Optional: cancel an in-flight turn, since the scene changed under it.
- **Unregister / quit** — unregister the drain timer, write `{"cmd":"shutdown"}`, close
  stdin. The child's read loop ends on EOF, so an idle orphan is impossible; its stdin
  reader thread plus bounded HTTP timeouts make it notice EOF even mid-read. Do not block
  Blender's shutdown on the child.

### Fallback

"Transport cannot start" and "endpoint cannot stream" are different failures:

- **Worker cannot start at all** (spawn fails, pipe error, no `ready`): **fail visibly.**
  There is nothing to degrade to — non-streaming still needs the same child. Show a
  persistent panel error naming the reason, disable Send, offer Retry; never retry in a
  loop.
- **Request fails** (DNS/TLS/401/429/drop/crash): keep the worker, show the error in the
  transcript for that turn, keep partial text marked incomplete, let the user resend. Per
  ticket 3 there is no automatic retry, and there should not be one for a stream.
- **Streaming specifically unsupported** by a compatible endpoint: a narrow per-request
  degrade to `stream=False` in the same child, rendering the reply in one shot. Optional,
  and not a substitute for the first case.

### What the choice changes elsewhere

- **Permissions/packaging:** none needed. 5.2's permission vocabulary is exactly
  `camera, clipboard, files, microphone, network`
  (`_bpy_internal/extensions/permissions.py`) — there is no subprocess permission — and the
  manifest already declares `network`. The only packaging change is that the worker module
  ships with the extension (the current `[build]` excludes only `__pycache__`, `.git` and
  zips, so it is included automatically). No new wheel; the child's `requests`/`certifi`
  are Blender's.
- **Stop/cancellation:** strictly better — an in-process cancellation flag becomes a flag
  *plus* a pipe message *plus* `proc.kill()` as the last resort.
- **Panel prototype:** the repaint path is unchanged and still correct. `stream.py`'s `_tick`
  stops calling the fake `session.advance()` and drains the worker pipe instead; tag-redraw
  only on change, self-unregister on idle. Scope grows by one file (`_worker.py`) and the
  failure states above.

**Human must ratify:** (1) accepting Blender's documented "unsupported" verdict on the
long-lived thread + repeating timer as decisive without field-testing its real crash rate;
(2) shipping and launching a standalone `_worker.py` via `sys.executable` rather than
`multiprocessing`; (3) `proc.kill()` as the terminal Stop path; (4) visible failure over any
silent non-streaming fallback.

## Comments
