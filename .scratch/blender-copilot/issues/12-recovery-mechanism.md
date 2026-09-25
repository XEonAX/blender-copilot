# What replaces undo as the recovery mechanism?

Type: grilling
Status: resolved
Blocked by: none

## Question

Undo turned out to be much weaker than the map assumed, while the design still auto-runs arbitrary code with no approval gate. Rebuild the recovery design around what undo actually does.

Established: Python-initiated operators never push undo; direct RNA writes push nothing; undo reaches only local `bpy.data`, only after an explicit push, and the push belongs at the **end** of the unit being reverted; undo never reaches filesystem writes or deletes, subprocesses, network calls, render/GPU state, screens/workspaces/WM, preferences, Python state, or linked-library data; and it is absent entirely when Global Undo is off.

Decide:

1. **Push discipline.** Per tool call, or per turn? What exact unit does Ctrl+Z revert, and is that the unit the user expects? Note that the push-at-end semantics change the answer the map originally assumed.
2. **Snapshot cadence.** Is `wm.save_as_mainfile(copy=True)` really the mechanism? When is it taken, what does it cost on a large scene, where does it live, and is it a genuine backstop or theatre?
3. **A before-image.** Should the addon record a cheap record of what the agent is about to touch, so it can *report* what changed given there is no reliable undo? What is the cheapest sufficient representation — `get_scene_info` output, an RNA path list, something else?
4. **Global Undo off.** How is that detected, and should the addon refuse to auto-run arbitrary code when it is, or warn loudly and proceed?
5. **Honesty in the UI.** What does the panel claim about what is *not* recoverable? A blanket "everything is undoable" is a lie; a warning on every run is noise. Find the true sentence.
6. **Does auto-run survive this?** If the honest conclusion is that no recovery mechanism can cover arbitrary code, say so plainly — and then say what that does to the no-approval-gate decision, which was made before these facts were known.

Read *What exactly happens when we exec model code, and what does undo cover?* first; it owns the facts. Fold in the empirical undo result the panel prototype is asked to confirm.

Deliverable: the recovery design, the push and snapshot rules, and the exact user-facing promise.

## Answer

**RATIFIED 2026-09-25 by the project owner — with one rejection.** Items 1–5 were
accepted as written. **Item 6 was rejected:** the approval gate is *not built* and
auto-run stays unscoped. See *Ratification* at the end of this file for what that
does and does not leave in force — the consequences are real and are recorded
there rather than smoothed over.

**Empirical status of the GUI confirmation this ticket was told to fold in.** Ticket *Build the cheapest installable extension that proves the panel* built `tools/undo_probe.py` but its own answer says the GUI run was “handed over rather than automated”, and I am forbidden to launch a GUI. The four-case confirmation is therefore **still outstanding**. Every undo claim below rests on ticket *What exactly happens when we exec model code, and what does undo cover?*’s live-GUI timer probes (`t_gui.py`, `t_recipe.py`) and its `-b` probes, plus two `-b` probes I ran for this ticket (isolated `BLENDER_USER_CONFIG=/tmp/bc-t12-cfg`, user config untouched) which establish:

- `sys.monitoring` exists with `LINE`/`INSTRUCTION` events in Blender’s bundled Python 3.13.13;
- `bpy.context.preferences.edit.use_global_undo` is readable, `undo_steps` defaults to 32, `undo_memory_limit` to 0;
- `depsgraph_update_post` fires on a bare RNA write and on `bpy.data.objects.new()`, and does **not** fire on a selection change — the only usable mutation signal, since `wm.bl_rna` exposes **zero** undo-stack properties;
- `bpy.data.is_dirty` stays `False` after RNA writes (re-confirms ticket 02), so it is not a mutation signal.

`tools/undo_probe.py` remains the human instrument; this ticket does not close it.

### 1. Push discipline — one step per turn, at the end, in a `finally`, only if the turn mutated

- **Unit = the user’s turn** (their message), never the tool call. After “make this thicker” ran four tool calls, one Ctrl+Z must revert all four; the user reasons in requests, and a half-applied turn (object created, material assignment lost) is worse than none.
- `bpy.ops.ed.undo_push(message="copilot: <first ~40 chars of the user turn>")` is called in the turn’s `finally`, after the last mutation, so it still runs after a partial/exceptioned turn. Pushing *after* is what makes Ctrl+Z land on the pre-turn step and revert exactly the turn (verified, ticket 02); pushing before reverts too far.
- **Baseline guard:** on the session’s first mutating turn, if the addon has never pushed, push a marker *before* the first mutation so a pre-turn step exists. The addon tracks this itself (no RNA for stack depth); after that the user’s own last step is the boundary.
- **Only if mutated.** Trigger = the `depsgraph_update_post` flag *or* a non-empty pre/post summary diff (below). A pure read turn must not push: an empty step makes Ctrl+Z look broken.
- **Never pass `undo=True`** to any `bpy.ops` the model calls; that fragments one turn into several interleaved steps.
- **State the cost:** one step per turn eats one of the default 32 undo slots per turn, so a chatty session evicts the user’s own history. Say it in the UI, do not hide it.

Rejected: per-tool-call pushes (fragments one request into N Ctrl+Z presses; unusable intermediates; evicts 32 slots N× faster). Push-at-start (reverts too far, verified). Trusting `bl_options={'REGISTER','UNDO'}` (dead for Python-called operators).

### 2. Snapshot cadence — `wm.save_as_mainfile(copy=True)` is not the mechanism

- Evidence: `undo_push` costs 0.001–0.005 s in RAM; the copy costs 0.058–0.070 s **and 31.9 MB on disk** on a 32 MB / 1M-vert scene, doubles with the `.blend1` rotation, runs `ED_editors_flush_edits` (a real side effect on the live session), blocks the main thread, and scales to seconds and gigabytes on a texture/cache-heavy production file (unmeasured). Undo cannot undo it, and `open_mainfile`/`read_factory_settings` clears the stack anyway.
- **So the recovery mechanism is the memfile undo stack, full stop.** The `.blend` copy is demoted to a durable escape hatch: preference **off by default**; when on, taken lazily **once per session, before the first mutating turn**, with `copy=True`, `relative_remap=False`, a unique filename in a private dir under `bpy.utils.user_resource('CONFIG', 'blender_copilot/recovery')`, path reported in the transcript, old copies pruned. Never beside the user’s file; never per turn.
- It covers exactly one gap undo cannot: process death or a hung `exec`. For a saved file the honest durable point is the user’s own Ctrl+S, and the panel should say that rather than silently write copies.

Rejected: automatic per-turn copy — blocking main-thread disk writes, 2× disk, and theatre: it pretends to be undo while protecting against a crash the user already accepts.

### 3. Before-image — the bounded `get_scene_info` summary, captured pre/post and diffed

- Not an RNA-path crawl (unbounded on a large scene; its schema is ticket *The three tools’ contracts*’ territory), and not “ask the model what it did” (a fallible narrator).
- Concretely: capture the same bounded summary `get_scene_info` returns (fields per ticket 06) before the turn’s first `run_blender_python` and after its last, then diff. The diff does three jobs: the **receipt** the user sees (“changed object `Cube.location`; new Empty `Target`; removed `Material.001`”), the **push trigger** (non-empty ⇒ push the turn’s step), and the **reconciliation** input for `undo_post`/`redo_post` after an external Ctrl+Z. The `depsgraph_update_post` flag is the free secondary signal for the push decision without a scene walk.
- **Honesty limit:** the summary sees datablock-level change, not sub-object data (mesh verts, image pixels). The receipt says “objects/data changed”, never “exactly these properties changed”. A property-level RNA diff is rejected as too expensive on huge scenes.

### 4. Global Undo off — refuse auto-run, do not warn-and-proceed

- Detect `bpy.context.preferences.edit.use_global_undo` (verified readable). With it off, `memfile_undosys_poll` declines and there is **zero** recovery for the only class we can recover, so the whole promise vanishes.
- Rule: while it is off, `run_blender_python` does not auto-run. The panel shows a banner with a one-click “Turn Global Undo on” (`preferences.edit.use_global_undo = True`) and an explicit per-turn “Run once anyway” for the user who consciously accepts no recovery. That gate is *degraded* mode, not normal mode.
- **Edit mode:** `ED_undo_is_memfile_compatible` declines and mode-specific undo takes over, so a push no longer snapshots the scene. Pending a probe, treat an active edit-mode object like Global-Undo-off (pause; offer “exit to Object mode”). This is a real usability cost and the item most likely to be wrong.

### 5. The true sentence

Put the coverage statement on screen **once, persistently**, and let each turn advertise the step it just created:

- Persistent, under the input, always: `Ctrl+Z undoes one agent turn. Changes outside this .blend — files, network, preferences — cannot be undone.`
- After a mutating turn, on the receipt line: `✓ Undoable · 3 objects changed · Ctrl+Z reverts this turn`
- Degraded-mode banner: `Global Undo is off — nothing the agent runs can be undone. Auto-run is paused. [Turn Global Undo on] [Run once anyway]`
- When an outside-the-blend effect is actually detectable, name it instead of warning generally. Save is detectable only as a `bpy.data.filepath` change (Save As); an overwrite save is not reliably distinguishable, and network/subprocess effects are not detectable at all — which is precisely why a persistent coverage sentence, not a per-turn claim, carries the non-coverage.

### 6. Auto-run with no gate does not survive — the gate moves to the capability boundary

Plainly: **no recovery mechanism can cover arbitrary code.** Undo reaches local `bpy.data` only, and only after a push. Code can delete files, spawn processes, hit the network (the manifest permission is a declaration, not a sandbox), overwrite plaintext preferences including its own API key, register timers/handlers that outlive the turn, and block the main thread forever with no supported cancellation (`while True:` is force-quit-only). None of that is recoverable; some of it is not even interruptible.

The no-gate decision was made when the map believed undo was a backstop. That belief is dead, so the decision does not survive **as stated**. What survives:

- Auto-run stays, but only for a **curated capability**: `run_blender_python` execs in a namespace that gives `bpy`/`mathutils`/compute stdlib and withholds `open`, `__import__` outside an allowlist, filesystem/process/network modules, `bpy.app.timers`/`handlers`, the filesystem-touching `bpy.ops` families (`wm`, `script`, `export_*`/`import_*`, `image.save*`), and `Text.write()`/`Image.save` methods. Then the only recoverable class of change (`bpy.data`) is the only class the agent can reach, and the promise in §5 becomes true rather than decorative.
- This is **blast-radius reduction, not a sandbox.** Python-level restriction is escapable, method-level and indirect-operator surfaces are easy to miss, and scene-derived prompt injection actively tries. The design must not call it security.
- Full-power code requires an explicit user action — per-run approval or a session toggle, **default off**. The gate did not disappear; it moved from “every call” to “the capability boundary”, which is also where a user can reason about it.
- Runaway protection: a per-turn budget enforced with `sys.monitoring` (`LINE`/`INSTRUCTION`, verified present) raising out of the code. It cannot stop a blocking C call, so a hang remains possible and the UI must not claim otherwise. Unverified — needs its own probe.

Follow-ups this ticket creates (the orchestrator graduates them): amend the destination sentence to “auto-runs **scene-only** Python; one Ctrl+Z per agent turn; the recovery copy is opt-in”; a ticket for the execution namespace + `bpy.ops` curation and an adversarial probe of its escapes; fold the mutation detector into ticket 06’s `get_scene_info` contract; run `tools/undo_probe.py` in a GUI.

### What a human must ratify

1. One undo step per turn (not per tool call), labelled with the user turn, pushed at the end.
2. The recovery `.blend` copy is **off by default**, opt-in, once per session — not the per-turn backstop.
3. The `get_scene_info` pre/post diff is the before-image, with its datablock-level (not property-level) honesty.
4. Global Undo off **pauses** auto-run; edit mode likely does too, pending the probe.
5. The exact UI sentences in §5.
6. The largest one: **dropping “auto-run arbitrary code with no approval gate”** in favour of “auto-run scene-only code; explicit approval for full-power code”, given the restriction is best-effort and not a sandbox.

## Ratification

**Ratified 2026-09-25 by the project owner.** Items 1–5 above were accepted as
written. **Item 6 was rejected, and the rejection changes the design.**

### Item 6 rejected: auto-run stays unscoped

The owner kept the original premise — **auto-run arbitrary Python, no approval
gate** — rather than demote auto-run to a scene-only capability with full power
behind a default-off gate.

That is coherent with the evidence rather than contrary to it. *The capability
boundary for model-authored code* built the proposed guard and attacked it: 77
attempts, 2 escapes, both by introspection rather than through any named path. **A
gate whose own probe shows it does not contain is arguably worse than no gate**,
because it buys false confidence. So this ticket's §6 claim — that "the promise
in §5 becomes true rather than decorative" — is moot, and the `Full access` gate is
not built.

### What the rejection leaves in force

Untouched, and still binding:

- §1's push discipline: **one unified push per user turn, at the end, in a
  `finally`, only if the turn mutated.**
- §3's receipt: the pre/post bounded `get_scene_info` diff.
- §4's Global Undo off → auto-run **pauses**. This is not a capability gate. It is
  the addon declining to auto-run when the user has switched off the only
  recovery that exists, and it stays.

### What is no longer true, and is now corrected in the map

The map said "auto-run for scene-only Python". It now says auto-run is unqualified.
Concretely, with no restriction installed:

- The push covers **local `bpy.data` only** — never files, subprocesses, network
  calls, preferences or Python state. Nothing else stands in front of those.
- Auto-run code has **full filesystem access**, and the sharpest consequence is
  *Where the API key lives, and how the user sets it* §1: the key is ratified to
  sit in **plaintext** in `userpref.blend`. Auto-run code can read it and send it
  anywhere. That is two ratified decisions combining, not a defect in either.
- `bpy.app.handlers` and `bpy.app.timers` are open to model-authored code, so a
  turn can register work that outlives the turn and the session. The end-of-turn
  push does not reach it and cannot.
- *The capability boundary for model-authored code*'s withhold list is **not
  enforced, because it is not installed.** Its measurement survives as evidence;
  its mechanism does not.

This is recorded as the owner's decision, taken with the measurement in hand. It
is not softened here, because this file is what later sessions read.
