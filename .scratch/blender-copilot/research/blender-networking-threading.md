# Blender extension → OpenAI-compatible API: off-main-thread streaming into the UI

**Question investigated (2026-09-25):** How does a Blender extension call an OpenAI-compatible
API off the main thread and stream the reply into Blender's UI without freezing it, given that
`bpy` is main-thread-only and `bpy.app.timers` only pump from the GUI event loop?

**Target:** installed Blender **5.2.2** at `/Applications/Blender.app`.

**Path shorthands used for citations**

| Alias | Real path | Version |
|---|---|---|
| `APP/` | `/Applications/Blender.app/Contents/Resources/5.2/scripts` | installed 5.2.2 (target) |
| `PY/` | `/Applications/Blender.app/Contents/Resources/5.2/python/lib/python3.13/site-packages` | installed 5.2.2 (Python 3.13.13, requests 2.32.3, urllib3 2.4.0) |
| `SRC/` | `/Users/user/Projects/blender` | **5.3.0-alpha clone, read-only reference** — C sources are cited from here because the installed app ships no C source |

Docs URLs are the **5.2** API docs where they exist (same minor as the target).
Established facts not re-derived here: Blender 5.2.2 / Python 3.13.13; `requests` + `certifi`
bundled, `httpx`/`openai` absent; TLS to `api.openai.com` reachable (HTTP 401 without key).

---

## Recipe

Blender has no sanctioned primitive for "background thread with a live UI", so the working shape
is: one **main-thread** owner of all Blender state, one **non-Blender** worker, and a
`bpy.app.timers` callback as the only bridge. Use a `queue.Queue` of plain Python values
(str/dict/list only — no RNA objects, no `bpy` types) as the single shared object, with the worker
producing and the timer callback draining it non-blockingly. The worker does `requests.post(...,
stream=True, timeout=(connect, read))`, calls `raise_for_status()` **before** iterating, walks
`resp.iter_lines()`, and runs the OpenAI SSE parse + tool-call accumulation rules (below); it also
checks a cancellation flag once per chunk. The timer callback (registered with
`bpy.app.timers.register(cb, first_interval=0.03, persistent=True)`) appends arrived text to plain
Python state, calls `area.tag_redraw()` / `region.tag_redraw()` (or
`workspace.status_text_set(...)`) only when the text actually changed, and returns the next
interval — returning `None` unregisters it. For the tool-call loop the worker pushes a
"tool requested" record and blocks on a second queue, the timer callback performs the `bpy` work on
the main thread and pushes the result back; the main thread never `join()`s or blocks on the worker.
Blender's own equivalent code takes the safer variant of the same idea: worker **plus** all the
blocking I/O and parsing live in a spawned helper **process**, and the main process drains a
`multiprocessing.Pipe` inside a timer callback (`APP/modules/_bpy_internal/assets/remote_library/listing_downloader.py:335-342,752-768`).

---

## 1. The supported off-thread pattern, and what is illegal off-thread

**The `queue` + `bpy.app.timers` recipe is real, but it is *not* the pattern Blender documents as
supported.** The threading page is explicit:

- Python threads "cause Blender to crash in hard to diagnose ways" and "Python threading with
  Blender only works properly when the threads finish up before the script does, for example by
  using `threading.Thread.join()`. In other words, they can only be used while the main Blender
  thread is blocked from running."
  — <https://docs.blender.org/api/5.2/info_gotchas_threading.html>; identical text at
  `SRC/doc/python_api/rst/info_gotchas_threading.rst:1-25`
- The page's own **"unsupported case"** example is precisely a repeating timer driven from a
  daemon thread (`threading.Timer(0.1, ...)`), described as: "may seem to work for a while, but end
  up causing random crashes or errors in Blender's own drawing code."
  — same doc, "Code Examples" section (§ "This is an example of an **unsupported** case, using a
  timer that runs many times a second"); `SRC/doc/python_api/rst/info_gotchas_threading.rst:74-91`

So a long-lived worker thread that touches no `bpy` and is drained by a main-thread timer sits in
documented-limbo: it is the de-facto pattern, but the only officially acknowledged safe uses are
(a) threads that finish before the calling script (joined), and (b) **the `multiprocessing`
module** ("For running Python code independently of Blender, it is recommended to use the
`multiprocessing` module" — same doc, "Alternative Approaches").

**What is ILLEGAL from the worker thread.** The doc's own comment inside the *supported* example:

> `# NOTE: While threads are running, no code (including the main thread) may use bpy or any Blender API - only standard Python or third-party modules.`

— `SRC/doc/python_api/rst/info_gotchas_threading.rst:62-63` (and the 5.2 URL above).

Concretely, off-thread you must not touch:

| Illegal off-thread | Evidence |
|---|---|
| `bpy` module access at all (`bpy.data`, `bpy.ops`, `bpy.context`, `bpy.path`, …) | gotchas note above (`info_gotchas_threading.rst:62-63`) |
| `bpy.context` (`window`/`area`/`region`/`screen`) | same note; context is per-thread/main-loop state |
| RNA reads/writes on datablocks (scenes, objects, meshes, collections) | "should be read from Blender in the same thread" — `SRC/doc/python_api/examples/bpy.types.RenderEngine.1.py:61-62` |
| Handlers that alter data touched by another thread | `SRC/doc/python_api/examples/bpy.app.handlers.2.py:5-11` (frame-change handler + viewport thread ⇒ "can cause a crash of Blender") |
| Assuming *your own* RNA property callbacks are main-thread | `SRC/doc/python_api/examples/bpy.props.0.py:13-17`, `bpy.props.4.py:12`, `bpy.props.5.py:28` ("these callbacks may be executed in threaded context") |

Safe off-thread: standard library, third-party modules (`requests`), and pure data (`str`, `dict`,
`bytes`, `mathutils` values — `mathutils` carries no `bpy` state).

**Which side the timer runs on (and that it never runs under `-b`):**

- `bpy.app.timers` callbacks are executed by `py_timer_execute`, which takes the GIL with
  `PyGILState_Ensure()` and calls the Python callable — `SRC/source/blender/python/intern/bpy_app_timers.cc:54-72`; registration goes to `BLI_timer_register` — `:110-120`.
- Timers are pumped from the window-manager event loop: `wm_event_timers_execute()` sets the first
  window as context and calls `BLI_timer_execute()` — `SRC/source/blender/windowmanager/intern/wm_event_system.cc:566-579`, invoked from `wm_event_do_notifiers()` — `:603`.
- `WM_main()`'s loop is `events → do_handlers → do_notifiers → draw` — `SRC/source/blender/windowmanager/intern/wm.cc:625-648`, and the event step asserts it is on the main thread
  (`BLI_assert(BLI_thread_is_main())` — `SRC/source/blender/windowmanager/intern/wm_window.cc:2218-2222`).
- Under `blender -b` the GUI main loop is never entered: background mode goes straight to
  `WM_exit()`, and `WM_main(C)` is only in the `else` (GUI) branch — `SRC/source/creator/creator.cc:654-681`.
  ⇒ timers never fire in background mode (matches the established facts).

**Bundled precedent (the honest answer to "name a bundled addon").** No bundled *addon* implements
"worker thread pushes to a queue, a timer callback drains it". The nearest shipped code, and the
one to copy, is core, not an addon:

- `bpy.app.timers` + IPC bridge: `APP/modules/_bpy_internal/http/downloader.py` runs the download in
  a spawned child (`_mp_context = multiprocessing.get_context(method='spawn')` — `:413-417`;
  `Process(...).start()` — `:664-675`) connected by a duplex `Pipe` (`:661`), with daemon
  rx/tx threads **inside the child** (`:1059-1060`); the main process drains that pipe from a timer
  callback — `APP/modules/_bpy_internal/assets/remote_library/listing_downloader.py:335-342` and
  `:752-768`. The same pattern is used by the asset downloader
  (`APP/modules/_bpy_internal/assets/remote_library/asset_downloader.py:249,337-344` — poll interval
  plus timer registration with the same `assert is_registered` guard).
- The bundled **addon** that does background network work (`bl_pkg`, the Extensions addon) copies
  that same design: it shells the network work out to a **subprocess**
  (`APP/addons_core/bl_pkg/bl_extension_utils.py:250-252`) and refreshes the UI from
  `bpy.app.timers.register(..., persistent=True)` (`APP/addons_core/bl_pkg/bl_extension_notify.py:587-604`).
  It does not use threads at all for that.
- The only bundled addon that *does* use threads is the FBX exporter, and only inside a blocking
  operation: `ThreadPoolExecutor` + `SimpleQueue`, entered/exited as a context manager
  (`APP/addons_core/io_scene_fbx/fbx_utils_threading.py:1-30,90-125`), with the explicit note that
  "`bpy` cannot be imported here".
- The other bundled addon that parallelises work, `ui_translate`, uses a **process** pool, not a
  thread pool, and its comment states why the child cannot be like the parent: "While on linux
  sub-processes are `os.fork`ed by default, on Windows and OSX they are `spawn`ed … spawned processes
  do not inherit the whole environment of the current (Blender-customized) python. In practice, the
  `bpy` module won't load e.g. So care must be taken that the callback passed to the executor does not
  rely on any Blender-specific modules" — `APP/addons_core/ui_translate/update_repo.py:88-97`.
  (A grep of all of `APP/` for `threading.Thread`, `ThreadPoolExecutor`, `SimpleQueue` and
  `concurrent.futures` returns only FBX, `ui_translate`, the core HTTP downloader and a `bl_pkg`
  test helper — i.e. **no bundled addon anywhere pushes results from a thread into a
  `bpy.app.timers` queue**.)
- Threads in addon code otherwise appear only in tests
  (`APP/addons_core/bl_pkg/tests/modules/http_server_context.py:15,89-92`).

---

## 2. Can the bundled `requests` do SSE (`stream=True`, `iter_lines()`)? Exact parsing rules

**Yes.** Bundled versions in the target: `requests 2.32.3`, `urllib3 2.4.0`, Python 3.13.13
(`PY/requests/__init__.py`; verified by import).

Mechanics, from the bundled source:

- `iter_content()` streams from `self.raw.stream(chunk_size, decode_content=True)` and maps transport
  failures: urllib3 `ProtocolError → ChunkedEncodingError`, `ReadTimeoutError → ConnectionError`,
  `DecodeError → ContentDecodingError`, `SSLError → RequestsSSLError`
  — `PY/requests/models.py:799-855` (raises at `:822-828`).
  Iterating a second time on a consumed response raises `StreamConsumedError` — `:838-840`.
- `iter_lines()` splits `iter_content()` output into lines and **keeps the incomplete tail in
  `pending`** across chunks, re-yielding `pending` after EOF — `PY/requests/models.py:857-892`. It is
  documented "not reentrant safe" — `:864`. Default `chunk_size = ITER_CHUNK_SIZE` (512).
- A line-oriented read is exactly what SSE wants, and it is *not* re-inventing chunk handling.

Parsing rules for OpenAI-style `data:` events — two layers:

**(a) SSE framing (WHATWG spec; openai-python implements the same, and requests' `iter_lines` is
compatible when the consumer handles blank lines itself):**

- Stream must be UTF-8; lines separated by CRLF, LF or CR —
  <https://html.spec.whatwg.org/multipage/server-sent-events.html> §9.2.5.
- Per line: a line starting with `:` is a comment (ignore; also used as keep-alive); otherwise split
  on the first `:` → `field`, `value`, and strip **one** leading space from `value`
  — §9.2.6.
- `data` appends `value` plus a LF to the data buffer; a blank line dispatches the event; an
  incomplete trailing event (no final blank line) is discarded — §9.2.6.
- Consequence: multiple `data:` lines in one event concatenate with `\n`. OpenAI sends one JSON
  object per event on a single `data:` line, so per-line handling is sufficient in practice — but it
  is *not* the general rule.
- `[DONE]` is **OpenAI-specific, not SSE**: OpenAI's own SDK treats it as "stop": the SSE decoder
  (`SSEDecoder.decode`, modelled on the WHATWG algorithm) builds a `ServerSentEvent`, and `Stream`
  breaks on `if sse.data.startswith("[DONE]"): break`
  — `openai/openai-python` `src/openai/_streaming.py` (class `Stream.__stream__`; `SSEDecoder.decode`
  carries the comment `# See: https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation`).
- The OpenAI guide describes the stream as "**data-only server-sent events**" and says to read the
  `delta` field rather than `message`
  — <https://developers.openai.com/cookbook/examples/how_to_stream_completions> (§ "2. How to stream a chat completion").

**(b) Payload accumulation (the exact rules, from OpenAI's first-party SDK):**

- Each chunk is `{"id", "object":"chat.completion.chunk", "choices":[{"index", "delta", "finish_reason"}]}`.
  Index the choice by `choice.index`, not `choices[0]`, if you care about `n>1`; the final
  usage chunk has `choices: []` and `usage` populated (only when `stream_options={"include_usage": True}`)
  — cookbook §2 and §4 (same URL).
- `delta` can contain `role` (first chunk), `content` fragments, `tool_calls` fragments, `{}`, or a
  `finish_reason`.
- **Text:** `content` fragments are **concatenated** (`''.join`); `None` deltas must be skipped —
  cookbook §3 (`collected_messages = [m for m in collected_messages if m is not None]`).
- **Tool calls:** deltas arrive in `delta.tool_calls`, an **array of fragments**; each fragment
  carries an **`index`** which is the slot in the final `message.tool_calls` array. OpenAI's
  reference accumulator indexes by it: `tool_call_snapshot = (choice_snapshot.message.tool_calls or [])[tool_call_chunk.index]`
  — `openai-python` `src/openai/lib/streaming/chat/_completions.py` (`_accumulate_chunk`), and emits
  the fragment as `arguments_delta=tool_call_delta.function.arguments or ""` (`_build_events`).
- The merge rules are in `openai-python` `src/openai/lib/streaming/_deltas.py` (`accumulate_delta`),
  verbatim:
  - `index` and `type` are **replaced, never concatenated** (`if key == "index" or key == "type": acc[key] = delta_value`).
  - strings concatenate (`acc_value += delta_value`); dicts recurse; numbers add.
  - lists of objects are merged **by `index`**, inserting on `IndexError` (`acc_value.insert(index, delta_entry)`),
    recursing otherwise. A list entry that is not a dict raises `TypeError`; a missing `index` raises
    `RuntimeError`.
  - Practical translation for a from-scratch parser: `slot = tool_calls.setdefault(frag["index"], {...})`;
    `id` / `type` / `function.name` arrive on the **first** fragment for that index and are assigned
    (not appended); `function.arguments` fragments are **appended to a string**.
- **The JSON in `function.arguments` is only valid once fully accumulated.** Mid-stream the
  accumulated string is frequently unparseable (`{"color":`, `{"color": "re`); parsing it per chunk
  is a bug generator. Verified locally (see "Local verification"): `json.loads` raises
  `JSONDecodeError: Expecting value` on `{"color":` and on `{"color": "re`, and parses only the
  fully rejoined string.
- **Truncation is signalled separately from bad JSON:** if the model hits the token limit,
  `finish_reason == "length"` and the SDK raises `LengthFinishReasonError`; `content_filter` raises
  `ContentFilterFinishReasonError` — `openai-python` `src/openai/lib/streaming/chat/_completions.py`.
  So "arguments don't parse" has (at least) three distinct causes: not-yet-complete, truncated
  (`length`), or a client-side accumulation bug.
- `json.loads("[DONE]")` raises (`Extra data`/`Expecting value`): the sentinel must be tested
  **before** `json.loads`, never inside it.
- Some OpenAI-compatible servers differ in ways the client must tolerate: `event:` lines,
  comment keep-alives, an absent `[DONE]`, an `{"error": {...}}` payload inside the stream (the
  OpenAI SDK raises `APIError` when a decoded `data` event is a mapping containing `"error"` —
  `src/openai/_streaming.py`), and non-`chat.completion.chunk` objects (the SDK skips them:
  `_is_valid_chat_completion_chunk_weak`, `.../chat/_completions.py`).

**Local verification (loopback, not `api.openai.com`).** Against the bundled interpreter
(3.13.13 / requests 2.32.3 / urllib3 2.4.0) with a 127.0.0.1 HTTP server that wrote a synthetic
OpenAI-style stream **cut into 17 arbitrary 37-byte fragments** (deliberately slicing mid-line and
mid-JSON), `iter_lines()` reassembled every line correctly, `content` accumulated to `'Hello'`, and
the two `function.arguments` fragments rejoined to a parseable `{"color": "red"}` while both
intermediate strings raised `JSONDecodeError`. The probe script was temporary and has been deleted;
no repository files were created.

---

## 3. Getting arrived chunks on screen (what the timer callback must call)

- **Repaint a region:** `region.tag_redraw()` — RNA wrapper `RNA_def_function(srna, "tag_redraw", "ED_region_tag_redraw")` at `SRC/source/blender/makesrna/intern/rna_screen.cc:639`; area equivalent at `:480`.
  `tag_refresh_ui()` is **not** a general repaint: it errors unless the region is `TEMPORARY`
  (pop-ups) — `SRC/source/blender/makesrna/intern/rna_screen.cc:408-414,641-643`.
- **Precedent of exactly this inside a timer callback:** `bl_pkg`'s `_ui_refresh_apply()` walks
  `bpy.data.window_managers → windows → screen.areas → regions` and calls `region.tag_redraw()` —
  — `APP/addons_core/bl_pkg/bl_extension_notify.py:456-470` — called from the timer body at `:504` and
  `:515`; the timer itself is registered with `persistent=True` at `:587-604`.
- **Cheap alternative:** `context.workspace.status_text_set("…")` (status bar) —
  `APP/addons_core/bl_pkg/bl_extension_ops.py:1358`, cleared with `status_text_set(None)` at `:1373`.
- **Where the text lives:** plain Python state (module globals, a handler instance) or a
  `bpy.props`/preference property; the `draw()`/`Panel.draw` code that reads it always runs on the
  main thread. Never hand the worker an RNA reference; hand it a plain `queue.Queue`.
- **Do not redraw unconditionally.** Blender's own code only redraws when something changed:
  "Avoid high CPU usage by only redrawing when there has been a change."
  — `APP/addons_core/bl_pkg/bl_extension_ops.py:1355-1358`.

**Safe frequency.** The main loop's floor is not your timer interval:

- When idle, the loop sleeps **5 ms** (`const int sleep_us_default = 5000;`) unless a pending timer
  asks it to wake sooner — `SRC/source/blender/windowmanager/intern/wm_window.cc:2218-2250`.
- Pending timers shorten that sleep, but with a deliberate catch: the sleep is computed with `ceil`
  because "using `floor` or `round` is more responsive, it causes CPU intensive loops that may run
  until the timer is reached, see: #111579" — same file, `wm_window_timers_process()` around
  `:2190-2206`.
- Each due timer fires **once per loop iteration** (`execute_functions_if_necessary()` walks the timer
  list and reschedules `next_time = current_time + ret`) — `SRC/source/blender/blenlib/intern/BLI_timer.cc:85-105`, so a tiny interval cannot buy you more than one call per iteration.
- Empirically, intervals used by shipped Blender code: **0.01 s** for the remote-asset-library
  downloader (`APP/modules/_bpy_internal/assets/remote_library/listing_downloader.py:220`),
  **0.05 s** first interval then **0.1 s** steps for `bl_pkg` notifications
  (`APP/addons_core/bl_pkg/bl_extension_notify.py:341-343`), and **0.1 s** for the modal-timer
  variant (`APP/addons_core/bl_pkg/bl_extension_ops.py:1321`).

Recommendation: **0.03–0.1 s** for streaming text (≈10–30 fps of text updates), redraw only on
change. `0.01 s` is proven to work in Blender's own code but is 2× the idle-sleep floor and costs a
full UI redraw per tick if you are careless; do not go below ~0.01 s, and never return `0.0`
continuously. Return `None` (or call `bpy.app.timers.unregister`) to stop, exactly as
`listing_downloader.py:731-732` does at shutdown.

**Registration gotcha (real Blender bug).** Registering a **bound method** as a timer needs a
workaround, and the result should be asserted:

```python
# Work around a limitation of Blender, see bug report #139720 for details.
self.on_timer_event = self.on_timer_event
...
if not bpy.app.timers.is_registered(self.on_timer_event):
    bpy.app.timers.register(self.on_timer_event, first_interval=..., persistent=True)
    # Double-check the registration worked, see #139720 for details.
    assert bpy.app.timers.is_registered(self.on_timer_event)
```

— `APP/modules/_bpy_internal/assets/remote_library/listing_downloader.py:296-297,335-342`
(same workaround in `asset_downloader.py`, `__init__`).

---

## 4. Cancellation by the user (Stop) — summary

Full detail in the **Cancellation** section below. Short version: a blocked reader **cannot** be
interrupted by `response.close()`, so "Stop" must be implemented as (a) a cancellation flag checked
per chunk plus bounded read timeouts, and optionally (b) `response.raw.shutdown()` (urllib3 API,
`SHUT_RD`) which *does* unblock the reader immediately. Blender's own downloader uses (a)
exclusively: per-chunk `periodic_check()` + `raise DownloadCancelled(...)` —
`APP/modules/_bpy_internal/http/downloader.py:220-221,304-305,323-325`, with the flag consulted in
`may_continue_downloading()` at `:1177-1189` and process shutdown (`threading.Event do_shutdown`,
`:1002`) bounded by join timeouts (`:1277,1282,1287`).

---

## 5. What each failure looks like with `requests` alone

`requests`' exception hierarchy is `PY/requests/exceptions.py:12-135`
(`RequestException(IOError)` → `HTTPError`, `ConnectionError`, `Timeout` → `ConnectTimeout`/`ReadTimeout`,
`ChunkedEncodingError`, `ContentDecodingError`, `StreamConsumedError`, …).

| Failure | What you get | Distinguishable from `requests` alone? |
|---|---|---|
| **Auth failure** (bad/missing key) | HTTP 401/403; `response.raise_for_status()` raises `requests.exceptions.HTTPError` with `.response.status_code` (`PY/requests/models.py:997-1023`). **Only if you call it before iterating** — the error body is not SSE, so skipping `raise_for_status()` turns it into garbage lines. Blender's own downloader calls `stream.raise_for_status()` immediately after `send(..., stream=True, ...)` — `APP/modules/_bpy_internal/http/downloader.py:228-237`. There is no pre-flight validation possible without a key. | **Yes** — but you must classify `401/403` yourself; `requests` only tells you the status code, not "bad key" vs "not authorized for model". |
| **Rate limit** | HTTP 429 → `HTTPError` (same call). `Retry-After` is available as `response.headers.get("Retry-After")` (header presence/format is server-side, not a `requests` concept). **`requests` will not retry it for you:** `DEFAULT_RETRIES = 0` and `Retry(0, read=False)` — `PY/requests/adapters.py:73,206-212`. | **Yes** for "this was a 429"; **no** for when it is safe to retry a *stream* (that is policy, and streamed tokens must be re-concatenated from scratch). |
| **Malformed tool-call JSON** | Nothing from `requests` — it is your parser: `json.JSONDecodeError` (subclass of `ValueError`) per `PY/python3.13/json/decoder.py`. Causes are indistinguishable locally: incomplete accumulation (still streaming), server truncation (`finish_reason == "length"` ⇒ `LengthFinishReasonError` in the OpenAI SDK), or your own accumulation bug. | **Partly.** `JSONDecodeError` tells you parsing failed; only `finish_reason` distinguishes truncation from incompleteness, and on some compatible servers you get neither. |
| **Dropped connection mid-stream** | `ChunkedEncodingError` (from urllib3 `ProtocolError`, e.g. "Response ended prematurely") or `ConnectionError` (from `ReadTimeoutError` → "Read timed out"), depending on the failure mode — `PY/requests/models.py:822-828`; urllib3 raises at `PY/urllib3/response.py:757-781`. A TLS-level drop during the read surfaces as `RequestsSSLError`/`SSLError`. | **Partly.** Exception *class* distinguishes timeout-ish from protocol-ish; the *cause* (proxy reset vs. server restart vs. Wi-Fi) is not distinguishable, and any text already emitted is lost unless you accumulated it yourself. Default retry is off (`Retry(0, read=False)`, `adapters.py:210`), so a broken stream is **not** silently replayed — good for correctness, bad for UX. |
| Server-reported mid-stream error | Some services send `data: {"error": {...}}`; OpenAI's SDK explicitly detects a decoded mapping with an `"error"` key and raises `APIError` — `openai-python` `src/openai/_streaming.py`. | **Yes**, if you check for `"error"` in each event *before* treating it as a chunk. |

---

## 6. Deadlock risk in the request → tool-call → request loop

**Where the boundary falls.** The loop crosses the boundary **twice per turn**:

```
main thread (bpy allowed)                 worker (no bpy, ever)
  operator starts run  ──start──────────►  requests.post(..., stream=True)
  timer cb: drain queue  ◄──events──────   iter_lines() + SSE parse + accumulate
  timer cb: bpy work (edit scene)         (blocks on tool-result queue)
  timer cb: push tool result ──────────►   next request with tool output
```

Rules that make this safe:

1. **Only plain data crosses.** A `bpy.types.Object` (or any RNA reference) cannot be sent to a
   worker: with threads it is unsafe to touch; with `multiprocessing` it is unpicklable. Blender's
   own downloader sidesteps this by making the child importable and self-contained — note
   `_mp_context = multiprocessing.get_context(method='spawn')`
   (`APP/modules/_bpy_internal/http/downloader.py:413-417`): on macOS the child is a fresh
   interpreter that inherits nothing, and the module has to be import-safe without `bpy` state
   being alive (cf. `APP/addons_core/io_scene_fbx/fbx_utils_threading.py:6-8`).
2. **The main thread must never block on the worker.** If a timer callback calls `queue.get(block=True)`,
   `Thread.join()`, `Pipe.recv()` without `poll()`, or `Process.join()`, the GUI stops redrawing (at
   best) and, in the tool-call ping-pong, deadlocks: the worker is waiting for a tool result that
   only the main thread can produce, while the main thread waits for the worker. Blender's own
   bridge is non-blocking on the main side: `update()` → `_handle_incoming_messages()` uses
   `self._connection.poll()` and breaks out — `APP/modules/_bpy_internal/http/downloader.py:735-760`.
3. **Can a worker-held lock block the main thread?** Yes — if the main thread's callback tries to
   acquire it. Any lock the worker holds across the HTTP read (e.g. a mutex around "shared
   conversation state") will stall the timer callback (`acquire()` on the main thread) for as long
   as the server takes to respond, i.e. a UI freeze that looks like a hang. The fix is to make the
   queue the only shared object: `queue.Queue` is internally locked and short-critical-section, so no
   user lock is needed at all. Blender's multiprocess bridge does use locks, but **only inside the
   child**, around its own queues (`threading.Condition download_cancel_queue_lock` —
   `APP/modules/_bpy_internal/http/downloader.py:997-1002,1184-1189`).
4. **The reverse direction is safe** but must be *cooperative*: the worker can hold the GIL
   (pure-Python JSON parsing) and thus delay the timer callback by up to the interpreter switch
   interval (`sys.setswitchinterval()`, default 0.005 s) — stutter, not deadlock. Socket I/O and
   TLS release the GIL, so the UI stays live during the network wait.
5. **Don't hold a lock while executing `bpy`.** A timer callback that takes a lock, runs `bpy.ops`
   (which may re-enter the event loop via a modal operator or a nested `bpy.ops` call) and only then
   releases it, can be re-entered by another timer tick; the lock is then held by the main thread
   against itself. Keep the critical section to "swap the pending text out of the queue".
6. **Lifecycle:** `bpy.app.timers` callbacks are removed on file load unless registered
   `persistent=True` (`SRC/source/blender/python/intern/bpy_app_timers.cc:74-88`;
   `BLI_timer_on_file_load()` → removes non-persistent — `SRC/source/blender/blenlib/intern/BLI_timer.cc:144-149`).
   A worker outliving its timer (file load, addon unregister, Blender quit) is the documented crash
   class (`info_gotchas_threading.rst:74-91`), so the unregister path must also signal the worker and
   let it exit; `bl_pkg` does exactly that ordering (unregister timer at `:732`, then
   `_bg_downloader.shutdown()` at `:745` and the done-callback at `:750` —
   `APP/modules/_bpy_internal/assets/remote_library/listing_downloader.py`).

---

## 7. What cannot be verified without a live API key

**Verified in this investigation (no key needed):** Blender 5.2.2's Python/requests/urllib3 versions;
SSE line reassembly and tool-call accumulation rules, against the bundled library over 127.0.0.1;
that `response.close()` does *not* unblock a blocked reader while `response.raw.shutdown()` does;
that a read timeout surfaces as `ConnectionError: Read timed out.`; TLS reachability to
`api.openai.com` (established earlier: HTTP 401 ⇒ TLS + DNS + egress OK).

**Cannot be verified without a key (do not treat as confirmed):**

1. Anything about the real wire format from `api.openai.com`: whether real chunks are exactly as the
   cookbook shows, whether `[DONE]` is always sent, how aggressively `function.arguments` is
   fragmented, whether `index` starts at 0 for every turn, whether `id`/`name` appear only once.
2. Error semantics: the real body/`error.code` shapes for 401/403/429, whether `Retry-After` is
   present and in which format, quota-exceeded vs. rate-limited distinctions, and moderation
   refusals mid-stream.
3. `stream_options={"include_usage": true}` support on the chosen (possibly non-OpenAI) endpoint, and
   its final `choices: []` chunk behaviour.
4. Tool/function schema acceptance, parallel tool calls (`index` > 0) behaviour, and whether the
   endpoint honours `tool_choice`.
5. Behaviour of third-party "OpenAI-compatible" servers (llama.cpp, vLLM, Ollama, LiteLLM, Azure):
   keep-alive comments, `event:` lines, missing `[DONE]`, chunking of `arguments`, `finish_reason`
   vocabulary. The client should be defensive precisely because this cannot be tested here.
6. Everything GUI-side: real repaint latency, whether the timer keeps firing while a modal operator
   or a render is active, multi-window behaviour, and whether `0.03 s` vs `0.1 s` is noticeably
   different on a given machine. This requires running the GUI; `blender -b` cannot test it at all
   (timers never fire — `SRC/source/creator/creator.cc:654-681`).
7. Long-run stability of the chosen threading strategy. The docs assert that daemon-thread patterns
   "end up causing random crashes" without saying at what rate; only real use (and crash reports)
   tells you whether the timer+thread variant is acceptable in practice for this addon, or whether
   the subprocess variant must be used. **This is the single largest unverified risk in the design.**

---

## Cancellation

**Findings, then policy.**

1. **`response.close()` does not interrupt a blocked read.** `requests.Response.close()` calls
   `self.raw.close()` and `release_conn()` — `PY/requests/models.py:1026-1034`; urllib3's
   `HTTPResponse.close()` closes the file-like object and the connection
   (`PY/urllib3/response.py:1080-1090`), but Python's own socket docs state that `close()`
   "releases the resource associated with a connection but does not necessarily close the connection
   immediately. If you want to close the connection in a timely fashion, call `shutdown()` before
   `close()`" — <https://docs.python.org/3/library/socket.html> (Socket Objects → `close()`).
   **Measured (loopback, bundled 3.13.13 + requests 2.32.3 + urllib3 2.4.0):** a reader blocked in
   `iter_lines()` whose `response.close()` was called from another thread at t=1.5 s did **not**
   wake; it returned only when the server closed the socket at t=30.0 s. `close()` is therefore not
   a Stop button.
2. **`response.raw.shutdown()` *does* interrupt a blocked read.** urllib3 exposes
   `HTTPResponse.shutdown()` → `self._sock_shutdown(socket.SHUT_RD)`
   (`PY/urllib3/response.py:1075-1078`; the callable is wired in `:626`). **Measured:** the same
   blocked reader returned at t=1.50 s — i.e. immediately at the moment `shutdown()` was called.
   Caveats: it is an **urllib3 API reached through `response.raw`** (not documented as part of
   `requests`), it must be called from a *different* thread than the reader, it raises `ValueError`
   if `_sock_shutdown` is unset, and the reader observes a **normal end-of-stream, not an
   exception** — so your own cancellation flag is still required to distinguish "stopped by user"
   from "server finished". (Note `models.py:888` also yields a final unterminated `pending` line
   at EOF, so a truncated last line can surface as a bogus partial `data:` payload if you do not
   keep the flag.)
3. **Timeouts are the portable mechanism, and they are per-read.** `requests` takes
   `timeout=(connect, read)`, normalised to urllib3's `Timeout` (`TimeoutSauce(connect=connect, read=read)`
   — `PY/requests/adapters.py:640-670`), and urllib3 applies it to the socket
   (`self.sock.settimeout(self.timeout)` — `PY/urllib3/connection.py:384,505`), i.e. **per socket
   operation, not for the whole stream**: a stream may run for hours as long as no single read gap
   exceeds `read`. Python semantics: "operations fail if they cannot be completed within the timeout"
   — <https://docs.python.org/3/library/socket.html> (Notes on socket timeouts). **Measured:**
   `timeout=(5, 1.5)` against a server that sent one event and then went silent raised
   `requests.exceptions.ConnectionError: … Read timed out.` after 1.51 s. Note the class: mid-stream
   read timeouts become `ConnectionError` (mapped at `PY/requests/models.py:826`), whereas a
   timeout waiting for the *initial response* raises `requests.exceptions.ReadTimeout`
   (`PY/requests/adapters.py:712-713`).
4. **Blender's own chosen strategy is cooperative cancellation between chunks, never mid-read
   interruption.** In `_bpy_internal/http/downloader.py`: a check before the request and after the
   response (`:220-221`, `:304-305`), then **inside the read loop**
   `while chunk := stream.raw.read(self.chunk_size): if not self.periodic_check(...): raise DownloadCancelled(...)`
   (`:323-325`), where the check consults a child-process shutdown `threading.Event` and a cancel
   set under a lock (`may_continue_downloading()` — `:1177-1189`; `do_shutdown = threading.Event()`
   — `:1002`; the callback is installed as `downloader.periodic_check = may_continue_downloading`
   — `:1201`). Shutdown then joins the child's threads with **bounded** timeouts
   (`join(timeout=0.25/1.0)` — `:1277,1282,1287`) so a wedged worker cannot hang Blender, and the main
   process reports "timeout waiting for background process top stop" (`:725`).
   Its reported cancel reason is a first-class value: `DownloadCancelled` (`:1811`).
5. **What happens to an abandoned stream?** If you neither cancel nor consume it: the socket/response
   stays open until GC or process exit, holding a pooled connection; the server keeps generating (and
   billing) until it notices. Re-iterating the response raises
   `StreamConsumedError` (`PY/requests/models.py:838-840`); iterating a *partially* consumed response
   resumes from where the generator stopped (the generator's state lives in the response object, which
   is why `iter_lines` must not be shared between threads — "not reentrant safe", `:864`). Because the
   default retry policy is `Retry(0, read=False)` (`PY/requests/adapters.py:73,209-212`), a dropped
   or timed-out stream is **not** silently restarted: partial output is what you have, and any
   continuation is a new request you must construct.

**Recommended Stop button (for this addon).**

1. Set a per-run `threading.Event`/flag owned by the main thread (e.g. `run.cancelled.set()`).
2. Worker checks it once per received line/chunk and exits the loop by itself
   (cooperative — the Blender-proven path).
3. To make the exit *prompt* during a silent stall, use a bounded read timeout
   (`timeout=(5, 1.0)` is a reasonable starting point) so a dead peer cannot pin the worker for more
   than a second, and additionally call `response.raw.shutdown()` from the main thread for an
   immediate unblock (accepting that it is an undocumented-in-`requests` urllib3 call, and treating
   the resulting clean EOF as "cancelled" because the flag says so).
4. Keep the timer alive until the worker has actually finished; do not unregister the timer and then
   leave results unread. On addon unregister/file-load: unregister the timer, set the flag, then let
   the worker exit on its own (never `join()` from the main thread; `join()` with a timeout only from
   a place that is allowed to block, or not at all).
5. Discard partial tool-call JSON on cancel: a half-accumulated `function.arguments` string is not a
   tool call.

---

## What I could not determine

- **Whether the timer + long-lived-thread recipe is actually safe in Blender 5.2.2.** The documented
  position (`info_gotchas_threading.rst`) explicitly forbids `bpy` use while threads run and calls a
  repeated-timer-from-a-thread "unsupported"; it neither blesses nor forbids the queue+timer variant.
  The only strong, shipped precedent (Blender 5.2's own
  `_bpy_internal/http/downloader.py`) does not use threads in the main process at all — it uses a
  spawned subprocess + `Pipe` + `bpy.app.timers`. Choosing threads is a judgement call that only
  field testing can settle; the subprocess route is the conservative one.
- **No `.blend`/API-key-free end-to-end run.** I did not run the flow inside Blender's GUI (no GUI
  session, and `blender -b` cannot fire timers — `SRC/source/creator/creator.cc:654-681`), and I did
  not call `api.openai.com` (no key). Streaming behaviour was validated only against a local
  loopback server.
- **Exact C-source fidelity to 5.2.2.** All C citations come from the read-only 5.3.0-alpha clone
  (`SRC/`), because the installed app ships no C sources. The Python-side citations (`APP/`, `PY/`)
  *are* from the installed 5.2.2. Where a claim depends on C behaviour (timer main-thread pumping,
  the 5 ms idle sleep, `WM_main` not running in background) the 5.2 docs page was used as a
  cross-check where one exists; the rest rests on the clone.
- **Real-world frequencies for the specific addon.** Whether 0.03 s vs 0.1 s matters, and how much
  the GIL contention from JSON parsing in a worker thread shows up as UI stutter, cannot be measured
  without the GUI.
- **Third-party "OpenAI-compatible" endpoint behaviour** (see §7.5): only a live key (or a captured
  trace from a real server) can confirm.
