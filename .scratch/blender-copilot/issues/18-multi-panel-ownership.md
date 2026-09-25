# Whose state is it when more than one panel is open

Type: grilling
Status: resolved
Blocked by: none

## Question

The map has carried "what happens with more than one panel open — several windows
or areas showing the same conversation, and whether the agent's state is per-panel
or per-process" as fog since charting. Two tickets have since made it sharp
enough to answer.

*How a conversation is laid out and controlled* made the panel stateful and gave
it a `boxes` layout with paging, a busy kind, and a Stop control backed by a
subprocess handle. *How a conversation degrades as context grows* then split the
world cleanly in two: the **store** (the user's record, one file per conversation,
lossless) and the **projection** (the degraded view sent to the model, derived at
request time, never written back). *Where chat history lives* scopes the store per
`.blend` path.

Nothing decides who owns the projection, the in-flight request, or the subprocess
when a second panel opens. Decide:

1. **The ownership rule.** Per-panel, per-conversation, or per-process. Name what
   actually breaks in the other two cases rather than asserting a preference:
   what happens to an in-flight turn, the Stop button's target, the undo push in
   *What replaces undo as the recovery mechanism?* §1, and the `Text` datablock
   that *How a conversation is laid out and controlled* binds the code companion
   to.
2. **Two panels, one conversation.** Is it a shared live view, two independent
   scroll positions over one store, or refused outright? The panel cannot scroll
   and pages instead (*What can a Blender 5.2 panel actually render and accept?*),
   so "two views of one store" has a concrete meaning here and is not free.
3. **Two panels, two conversations.** Permitted? If yes, does a second concurrent
   agent run, or does the second panel attach to the running one? Note this is the
   case that makes the global-undo interaction in *What replaces undo as the
   recovery mechanism?* ambiguous: one push per turn presumes one turn at a time.
4. **The subprocess.** *How the addon talks to the API* chose one `_worker.py`
   child per... nothing said. Per panel is one HTTP client per panel; per process
   is a shared client with a queue. Pick, and say what happens to the other
   panel's stream when one panel's Stop kills the child — the failure mode the
   ticket names as `proc.kill()` being terminal.
5. **The cheap answer, if it is defensible.** Given the slice's target, "one panel
   at a time; opening a second attaches or refuses" may be the right call and
   costs nothing to build. If you take it, say what the panel shows in the second
   area, and check whether Blender makes refusal even possible — an addon cannot
   register a space, and the panel is a sidebar in every 3D Viewport, so a second
   instance may not be preventable at all. If it is not preventable, this becomes
   a *must handle* rather than a *may choose*.

Deliverable: the ownership rule, the behaviour for each of the four cases, and
the amendment to *Where chat history lives* or *How the addon talks to the API* if
the answer changes what they own.

## Answer

**PROVISIONAL — Type: grilling, no human present.**

**Ownership rule: per-process. One conversation, one in-flight turn, one
`_worker.py` child, one undo push, one timer, one `Text` datablock per Blender
process. The panel owns nothing but what it draws.** There is no per-panel model
and, today, no *distinction* between per-process and per-conversation: a process
has exactly one main database and therefore one `.blend` path, and *Where chat
history lives* §2/§3 gives that scope exactly one rolling conversation.

### 1. Why the process is the owner

- **The store already is per-process.** Its key is `bpy.data.filepath`, a single
  value; opening another `.blend` *replaces* the main database. Ticket 04's
  `load_post` generation counter ("drop late chunks for the old file") is only
  coherent with one live conversation per process.
- **The transport is already per-process.** `bpy.app.timers.register(function,
  *, first_interval, persistent)` takes no window/area argument — verified on the
  installed 5.2.2 — so ticket 11's drain timer is process-global by construction.
- **Undo is process-global.** There is one stack, and ticket 12 §1's unit is *the
  user's turn, pushed once at the end in a `finally`*. That unit is only
  well-defined if turns are serialized process-wide.
- **`bpy.data.texts` is process-global**, so ticket 08's `Copilot Code` /
  `Copilot Transcript` are process singletons.
- **The repaint path is already plural.** `stream.tag_view3d_redraw()` walks every
  window → screen → `VIEW_3D`/`UI` region (`blender_copilot/stream.py:28-46`), so
  "all panels redraw" is existing behaviour, not new code.

What breaks in the *other two* cases, concretely:

- **Per-panel model.** (a) Two writers to one store scope: both persist
  `active.json`/the conversation file for the same `bpy.data.filepath`, last
  writer wins, and each panel's transcript drifts from the file and the other.
  (b) Two in-flight turns → two `finally` pushes whose mutations interleave, so
  neither step reverts the user's turn — Ctrl+Z reverts a *mixture*, silently,
  which destroys ticket 12 §1. (c) Two children, two Stop paths, and a
  `proc.kill()` that is terminal and therefore ambiguous. (d) Two companion
  datablocks collide: verified on the installed build that `bpy.data.texts.new(
  "Copilot Code")` called twice yields `Copilot Code` + `Copilot Code.001`, so
  `Show code` from one panel leaves the other editor stale and datablocks
  accumulate. (e) Two inputs on one draft — `prompt_text` is an RNA preference
  (`panel.py:118`), so the draft is shared whether or not we want it to be.
- **Per-conversation.** Not wrong so much as premature: it is the same thing
  today, and as soon as ticket 04's deferred named-thread picker lands it would
  license two *concurrent* turns and re-open (b). Conversations own **content**
  (ticket 04 already says so); the process owns **execution**.

**The one genuinely view-local candidate is rejected.** The paging cursor
(`Conversation.page`, `conversation.py:119`; `page_view`, `conversation.py:193`)
stays **shared**, so a panel is a pure function of process state. Two independent
cursors over one store were rejected because (i) the panel cannot scroll (ticket
01), so "two views" can only mean two different *pages*, i.e. visually two
different conversations; (ii) `bpy.types.Panel.__slots__ == ()` on the installed
5.2.2 and panels are instanced *per region* in C (`ARegion.panels`,
`DNA_screen_types.h:846`; `panel_add_instanced`, `interface_panel.cc:231`), but
whether Blender keeps one Python wrapper per panel across draws is not
verifiable without a GUI, and no handler fires when a region closes, so a
pointer-keyed cursor registry cannot be pruned reliably.

### 2. Two panels, one conversation → shared live view, one page

Same transcript, same page, same controls. `Older`/`Newer` in either panel moves
both, because there is one cursor; expand/collapse is per-message in the shared
conversation, so a toggle in one panel shows in the other. The second area shows
exactly what the first does, plus one honest line — `shared view — also open in
N other viewport(s)`, where N is counted at draw over `window_manager.windows`
the same way `tag_view3d_redraw()` already walks them. The line exists so the
shared paging is explained rather than looking broken.

### 3. Two panels, two conversations → not permitted in this slice

It cannot arise: one `.blend` per process (above), one conversation per `.blend`
scope, picker deferred (ticket 04 §3). When the picker lands, two conversations
may be *displayed* but turns stay serialized process-wide — the worker, timer and
undo push do not become per-conversation.

A second concurrent agent run is not refused by policy, it is **unrepresentable**:
ticket 08 §6 already replaces Send with Stop while busy, and that is true in
*every* panel, so there is no widget anywhere that could start a second turn.
That is also the answer to the undo ambiguity this ticket raises — one push per
turn presumes one turn at a time, and per-process ownership is what preserves
that presumption instead of a new rule that has to be enforced.

### 4. The subprocess → one child per process, shared

Whichever panel sends first spawns it (ticket 11's lazy start); one drain timer,
registered once, `persistent=True`; at most one in-flight request.

**`proc.kill()` terminality dissolves rather than being handled.** With one child
and at most one stream, kill fails exactly the one in-flight turn. There is no
"other panel's stream" to orphan — that is precisely what per-process ownership
buys. Ticket 11's rules apply unchanged: a killed child is observed as
`returncode == -9`/stdout EOF, the turn is marked failed, partial text stays
marked incomplete, and a fresh child is spawned lazily on the next send. The
failure is written into the shared conversation (`KIND_ERROR`, ticket 08), so all
panels render the same failure with no per-panel reset.

Rejected: a child per panel — N TLS handshakes/certifi contexts, N pipes, N
drain timers, and a Stop that must first decide which child it means. Rejected
also: a multiplexed child with request ids — needed only for concurrent turns,
which §3 makes unrepresentable.

### 5. The cheap answer, taken in the *attach* form

A second panel is **not preventable**: a panel registers a *type*
(`bl_space_type="VIEW_3D"`, `bl_region_type="UI"`, `panel.py:283-285`) and every
area with that region and category draws its own instance. An addon cannot
register a space, cannot close a sidebar, and cannot cap the instance count.

**Refusal is technically possible but rejected.** `poll(cls, context)` is invoked
once per region during that region's panel layout with that region's context
(`area.cc:3437`; the Python call is `rna_ui.cc:110-134`, and note the poll
pointer is built with a **dummy** panel, `RNA_pointer_create_discrete(nullptr,
..., nullptr)`), and a False removes the panel from that region — and, because
poll runs *before* `region_panels_collect_categories` (`area.cc:3575`), also
removes the `Copilot` tab there if it is the only panel in the category. But:
(i) poll is a classmethod with no panel identity, so ownership needs a
module-level registry keyed by region pointer; (ii) Blender fires no handler when
a region/area closes, so the registry can only be pruned during a draw and a
freed region's pointer can be recycled — the owner can flap; (iii) "which region
is first" depends on draw order, which is not a contract. So refusal costs a
fragile ownership registry to produce the *worse* result (Copilot silently absent
from the second viewport). Attach costs nothing because it is already the
default: the panels are the same state. **So this is a must-handle, and the
handling is attach** — not "one panel at a time" but "one *turn* at a time, N
identical views".

### 6. Amendments (recorded here; not edited into the other tickets)

1. **`Where chat history lives` (owns the store)** — add to §2/§3: the scope key
   is `bpy.data.filepath` and a process has one main database, so there is
   **exactly one live conversation per process**; the store is written by the
   process, never by a panel. No file or schema change.
2. **`How the addon talks to the API` (owns the child)** — add to Lifecycle
   rules: **one `_worker.py` per process** (not per panel, not per conversation),
   one drain timer registered once, at most one in-flight request process-wide,
   and therefore `proc.kill()` fails exactly one in-flight turn; the "other
   panel's stream" case does not exist. Start/stop/restart/crash rules otherwise
   unchanged.
3. **`How a conversation is laid out and controlled` (owns the panel)** —
   additive: the `shared view — also open in N other viewport(s)` header line
   when N ≥ 1; the statement that paging is process-global (both panels move
   together); and that `running code — cannot be interrupted` and Stop are facts
   about the process, so both appear in every panel. No new message kind, no new
   operator.
4. **`How a conversation degrades as context grows`** — no change; note only that
   per-process ownership means exactly one projection is alive per request, so
   §6's "panel shows the store, model sees the projection" needs no per-panel
   attribution.
5. **`What replaces undo as the recovery mechanism?`** — no change; §1's unit
   survives *because* turns are serialized per process.

### 7. Verified, and not

Verified on the installed 5.2.2, headless (`blender -b --factory-startup`,
probes in `/tmp`): `bpy.types.Panel.__slots__ == ()`; a `Panel` subclass does get
its own `__dict__`; `bpy.types.Panel()` is not constructible from Python
(`TypeError`), so per-instance persistence is unobservable headlessly;
`bpy.app.timers` exposes only `register`/`unregister`/`is_registered` with no
window argument; `bpy.data.texts.new` disambiguates a duplicate as `.001`;
`bpy.data.filepath == ''` and exactly one `window_manager` under
`--factory-startup`. The per-region panel instancing and the per-region poll are
read from the read-only 5.3-alpha clone's C (`interface_panel.cc:231`,
`area.cc:3437`, `rna_ui.cc:110`) — stable, old machinery, but **not** observed in
5.2.2's GUI, which I may not launch. Unverified by hand, for a human: that a
second viewport really draws a distinct instance; whether two `textbox` widgets
on one RNA property behave (the draft is shared; which cursor wins is unknown);
the look of the mirror line.

**What a human must ratify (one line):** that state is owned **per-process** —
one conversation, one in-flight turn, one worker child, one timer, one undo push,
one `Text` datablock — and that a second panel therefore **attaches as an
identical shared view with a shared paging cursor** (mirror header line, shared
Stop/Send/Clear, no independent cursors, no refusal), with two conversations
disallowed until ticket 04's deferred picker lands and turns serialized
process-wide even then.
