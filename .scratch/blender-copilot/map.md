# Map: Blender Copilot chat panel

`wayfinder:map` — the canonical artifact for this effort. Tickets are child issues under `issues/`. This map is an **index**, not a store: a decision lives in exactly one place, its ticket, and this map only gists it and links.

## Destination

A working vertical slice, running in Blender 5.2.2: an extension that opens a chat panel **inside Blender**, runs a multi-tool agent loop against an OpenAI-compatible API, and executes model-authored `bpy` in-process — with a per-call undo step so any run can be walked back with Ctrl+Z.

## Notes

**Domain.** A Blender extension (Python 3.13.13, bundled with Blender) that hosts its own agent loop. Target is the **installed** `/Applications/Blender.app` = **5.2.2**. The 5.3.0-alpha source clone at `/Users/user/Projects/blender` is **read-only reference**; it is unbuilt and off-limits.

**Execution is carried into this map.** Wayfinder plans by default, but this effort's destination is explicitly *a working vertical slice*, so tickets here legitimately build code rather than only deciding.

**Standing decisions settled while charting** (binding on every session; not tickets, so not in Decisions-so-far):

- **The agent loop runs in Blender's Python process.** No Node sidecar, no socket, no file-mailbox bridge, no IPC. Live `bpy.context` is free by construction — prior art needed an entire bridge plan to get what this design gets for nothing.
- **HTTP via bundled `requests` + `certifi`.** One OpenAI-compatible backend. *Verified:* Blender's bundled Python reaches `https://api.openai.com/v1/models` (HTTP 401 = TLS and reachability fine). `httpx`/`openai`/`anthropic`/`keyring` are **not** bundled; `requests`/`certifi` **are**. No wheel needed.
- **Three tools:** `run_blender_python` (workhorse), `get_scene_info`, `get_rna_info`.
- **Auto-run, no approval gate** — *intent only; the mechanism is reopened.* Recovery was to be a per-call undo step plus a scratch `.blend` snapshot before a turn's first mutation. Research showed the push is **never automatic**, so the mechanism now lives in *What replaces undo as the recovery mechanism?*. The no-gate assumption is itself on that ticket to defend or drop.
- **Context:** minimal live summary injected per turn; detail pulled by tool call. Never dump the scene.
- **Proper extension from day one:** `blender_manifest.toml`, `[permissions] network`, `blender_version_min = "5.2.0"`.
- **Hard constraints to design around:** `bpy` is main-thread-only; `bpy.app.timers` and modal operators only pump from the GUI event loop (never under `blender -b`); an addon **cannot register a new space type**, only attach to existing ones.
- **Verified the hard way, after the research tickets closed** (detail in Decisions so far): Python-initiated operators **never push undo**; `bpy.app.online_access` and even `--offline-mode` do **not** block Python sockets, so the manifest's network permission is a *declaration, not a sandbox*; `UILayout.textbox()` **does** exist in 5.2.2 and is our multi-line input; `UILayout` has **no rich text** in 5.2.2, so code and tool output render as plain text.
- **Two decisions were reopened by that research** and are now tickets, not premises: transport (*How the addon talks to the API: worker thread or subprocess?*) and recovery (*What replaces undo as the recovery mechanism?*). Do not build on either until it closes.

**Skills every session should consult.** HITL tickets: `grilling` + `domain-modeling`. Research tickets: `research`. Prototype tickets: `prototype`. Take them one ticket per session.

**Tracker:** local-markdown, rooted at `.scratch/blender-copilot/`. Check `research/*.md` before asserting a fact; several are already verified against source or by running Blender.

**Refer to every map and ticket by name**, never by bare number.

## Decisions so far

<!-- the index: one line per closed ticket, enough to judge relevance, then zoom the link for detail the ticket holds -->

- [What can a Blender 5.2 panel actually render and accept?](issues/01-panel-mechanics.md): host the panel in the 3D Viewport sidebar; repaint from a timer ending in `region.tag_redraw()`; `UILayout.textbox()` is a real multi-line input; panels cannot scroll and have no rich text.
- [What exactly happens when we exec model code, and what does undo cover?](issues/02-code-execution-and-undo.md): Python-initiated operators never push undo; undo reaches only local `bpy.data` after an explicit push; network permission is a declaration rather than a sandbox — so the recovery design had to be reopened.
- [How do we call the API off-thread and stream the reply into the UI?](issues/03-networking-and-threading.md): worker thread plus a queue drained by a timer, with `requests` SSE and fragmented tool-call accumulation — but the docs forbid `bpy` during threads and bundled code uses a subprocess, so transport needs its own decision.
- [Build the cheapest installable extension that proves the panel](issues/07-panel-shell-prototype.md): the panel exists and installs in Blender 5.2.2; sidebar host, `textbox` input and a change-only repaint timer all verified by running Blender. The visual judgement moved to the conversation-UX ticket.

## Not yet specified

<!-- in-scope fog you can't ticket yet; each patch graduates as the frontier reaches it -->

- **How a conversation degrades as context grows** — summarisation/compaction policy, and whether old tool output is dropped first. Sharper once *Where chat history lives* lands.
- **How the panel shows what the agent changed in the scene.** There is no textual diff for `bpy` mutations; something visual or reconstructable may be needed. Sharper once *Panel conversation UX* lands.
- **Whether the agent should see the viewport** — screenshots or render feedback rather than only scene data. Deliberately deferred; only meaningful once the panel and loop exist.
- **Whether Blender's own Python surfaces** (Text Editor, Python Console) should be reused or ignored once the panel works.
- **What happens with more than one panel open** — several windows or areas showing the same conversation, and whether the agent's state is per-panel or per-process.
- **Whether chaining models or base URLs mid-conversation is safe**, given providers differ in how they format tool calls already present in the history.

## Out of scope

<!-- work ruled beyond the destination; closed forever unless the destination is redrawn -->

- **Any Node/TypeScript sidecar, or reusing `pi-mono` / `veoery/BlenderAgent`'s runtime.** Judged beyond this slice: the in-process Python loop reaches the destination without IPC, and forking a TS monorepo buys machinery (managed workspaces, iteration artifacts, render critique) the slice deliberately excludes. Returns only as a fresh effort if the in-process loop proves insufficient. (A *Python* worker subprocess carrying only HTTP is a different thing and is live in *How the addon talks to the API: worker thread or subprocess?* — that is transport, not an agent runtime.)
- **Headless / `blender -b` operation.** The design depends on the GUI event loop — timers and modal operators never fire in background mode — so a panel-driven agent cannot work there by construction.
- **A VS Code extension, or any chat client outside Blender.**
- **Modifying the Blender source tree** — C++ or in-tree Python.
- **Rendering and visual-iteration tools** (`save_view`, `render`) and the workspace/iteration artifact model.
- **Multi-provider abstraction** (Anthropic-native, local models). One OpenAI-compatible backend only.
- **Blender 5.3-alpha support** and multi-version compatibility shims.
- **Distribution** — publishing to extensions.blender.org, supporting other users, token/cost accounting.
