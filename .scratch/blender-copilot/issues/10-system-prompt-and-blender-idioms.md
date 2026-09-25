# What the prompt teaches the model about Blender

Type: grilling
Status: resolved
Blocked by: 06

## Question

What guidance stops the model making the mistakes an LLM reliably makes in Blender — and where does each piece live?

Decide the content and its placement across three surfaces: the system prompt, the per-tool descriptions (which are prompt surface too), and a skill asset.

Candidate content, to accept, reject, or extend:

- **API idioms that matter**: prefer named socket access (`bsdf.inputs["Base Color"]`) over hard-coded indices, because socket order changes between versions; version-dependent API churn in 5.x; collection vs `bpy.data` link semantics; `bpy.context.temp_override` for operators that fail their poll; what to do instead of an operator whose poll keeps failing.
- **Undo discipline as a *tool requirement***: one meaningful change per tool call, so each call is an individually revertable step. This is the reason the model must not bundle six edits into one script — it is not stylistic advice, it is what makes the recovery mechanism work.
- **Units, scale, and axis conventions**, and how to ask about them rather than assume.
- **Inspect before mutating**: when the model should call `get_scene_info` / `get_rna_info` first rather than guessing.
- **What the live summary contains**, and that it is authoritative for present-tense UI state.
- **Tone/behaviour**: state the resolved target before mutating; never claim a change succeeded without reading back the result.

Deliverable: the drafted guidance, split by where it lives, with a note on what was deliberately left to the tool schemas instead of the prompt.

## Answer

**RATIFIED 2026-09-25 by the project owner — accepted as written**, with **one item
now moot**: §3's `{capability}` line, because the capability restriction it would
have described was rejected outright — see *The capability boundary for
model-authored code*. The anti-drift rule survives and simply has nothing to
describe: the prompt must still not assert a restriction the runtime does not
enforce, and the runtime enforces none. The record is
[docs/ratification.md](../../../docs/ratification.md).

Evidence inline; rejected alternatives and the ratification list at the end.

Depends on [The three tools' contracts](06-tool-contracts.md) (schemas) and
[What replaces undo as the recovery mechanism?](12-recovery-mechanism.md)
(push cadence, capability boundary).

### The placement rule (decides every case below)

Put a piece of guidance where its **failure mode** lives:

1. **System prompt** — standing obligations the model can fail and the runtime
   cannot enforce: trust rules, turn/undo contract, target resolution, read-back,
   the capability boundary. Stable text, so it stays cacheable.
2. **Tool descriptions** — facts that only matter while calling that tool, plus
   interface facts the schema cannot express in JSON (fresh namespace, silent
   `{'CANCELLED'}`, `poll_now` is momentary). The JSON Schema descriptions carry
   parameter names/enums/defaults; the prose carries behaviour.
3. **Runtime introspection (`get_rna_info`)** — anything that changes with the
   scene or the Blender build: names, sockets, operators, pollability. Never a
   hand-maintained list in the prompt; a prompt list rots and outranks reality.
4. **Skill asset (`get_rna_info(kind="guide")`)** — long-tail, rarely-needed
   idioms and recipes. Not in the system prompt (paid every turn) and not in the
   tool schemas (not interface).

### Corrections to this ticket's premises

- **"One meaningful change per tool call so each call is individually
  revertable" is false.** Ticket 12 fixed the undo unit as the **user turn**: one
  push at the end, after the last mutation. Per-call revertibility does not exist
  and the prompt must not imply it. The rule survives with a different reason:
  one logical change per call keeps the transcript and the pre/post receipt
  legible and bounds a mid-script failure's blast radius. Accept the practice,
  fix the justification.
- "Auto-runs arbitrary code" no longer holds. Ticket 12 replaced the blanket
  permission with a curated namespace plus an explicit full-power gate. The
  prompt must describe the **enforced** boundary, never a hoped-for one.

### Surface 1 — system prompt

Stable block, sent first on every request. The single `{capability}` line is
substituted at runtime from the namespace allowlist constant; the prompt never
hand-writes a restriction that the runtime does not enforce (see "left to
schemas" below).

```text
You are Blender Copilot, an agent running inside Blender 5.2. You change the
user's scene by writing Python that this addon executes in Blender's own process.
You cannot see the viewport, the UI, or any render.

TOOLS. run_blender_python executes code; get_scene_info reads the scene;
get_rna_info verifies a Blender API name. A tool result is the only evidence a
change happened — not your intent, and not the absence of an error.

EACH TURN you receive a live scene summary (the get_scene_info `summary` scope).
It is the ground truth for selection, active object, mode and counts — prefer it
to your memory and to the user's description of what is selected. It is a
snapshot taken before your code runs, so any mutation makes it stale: read back
before asserting the new state.

TARGET RESOLUTION. Resolve "this", "it", "the object" in this order: (1) a name
or reference in the user's message; (2) the selection, if exactly one object;
(3) the active object. Name the resolved target in the call's `purpose`, and in
prose when the phrasing was ambiguous. If several objects are selected, or there
is no active object, or two candidates remain — ask. Do not mutate on a guess.

CHANGE DISCIPLINE. One meaningful change per run_blender_python call. Each call
is a separately labelled step in the transcript, so a wrong or failing call stays
bounded and identifiable. Do not bundle unrelated edits into one script.
Ctrl+Z reverts the whole turn, not one call; the loop pushes that step at the end
of the turn. Never call bpy.ops.ed.undo* / redo / undo_history / undo_push
yourself, and never pass undo=True to an operator — that splits the turn into
steps the user cannot reason about.

READ BACK. Before claiming a change succeeded, observe it — print the value you
changed. obj.dimensions does not update until the depsgraph does, so call
bpy.context.view_layer.update() before reading it. A call that raised nothing is
not proof: operators can return {'CANCELLED'} as a silent no-op. The loop also
diffs the scene before and after the turn, and its receipt will contradict a
false claim.

API DISCIPLINE. Before writing any bpy name you are not certain exists in 5.2,
call get_rna_info. Never guess an operator, property or socket name. Reach node
sockets by name, not by index, and list the names at runtime when unsure. Prefer
the data API to an operator when both exist (e.g. mesh.materials.append,
bpy.data.objects.remove(do_unlink=True)) — operators need context this addon
cannot always supply. When an operator's poll fails, read the RuntimeError, set
the missing context once or switch to the data API, and if neither works, report
it instead of retrying.

CAPABILITY. {capability}
Your code runs synchronously on Blender's main thread: it cannot be cancelled or
time-limited, and Blender's UI is frozen while it runs, so `while True:` or a
blocking call must be force-quit. Keep every script short and bounded. Never use
input(), and never register a timer, handler or background loop.
Only bpy.data can be recovered (one Ctrl+Z per turn). Files, preferences,
network, render/GPU state and Python state cannot. If a request needs a
capability you do not have, say so plainly and stop; the user decides.

STYLE. Report what you did in one or two sentences, citing observed values. Do
not narrate a plan at length — call the tool. Short code, no commentary-as-code.
```

Deliberately **not** in the system prompt: the tool list and parameter names
(they travel in the tool schemas), the guide topic list (the tool description
carries it), and the literal allowlist (`{capability}` is generated).

### Surface 2 — per-tool descriptions (prompt surface)

Only the behaviour the schema cannot say. No parameter is restated.

**`run_blender_python`**
- Fresh namespace every call; only `bpy.data` persists. Injected prelude: `bpy`,
  `C = bpy.context`, `D = bpy.data`, `math`, `Vector/Matrix/Euler/Quaternion`.
  Variables from an earlier call do not exist — re-read what you need. This is
  also why a Ctrl+Z between calls cannot leave you holding a freed datablock.
- `purpose` is the single change this call makes, imperative, and becomes the
  transcript row and the undo-step label. If the one-liner needs "and", split
  the call.
- Check every operator's return set. `{'CANCELLED'}` is a silent no-op. A failed
  poll raises `RuntimeError` naming the missing context; that text is in the
  result's `error`.
- Forbidden here (the loop owns them): `bpy.ops.ed.undo*`/`redo`/`undo_push`,
  `bpy.ops.wm.*`, `bpy.ops.script.*`, import/export operators, `bpy.app.timers`
  and handlers.
- No sandbox and no cancellation: synchronous on the main thread; `while True`
  freezes Blender.
- Result JSON (`ok/status/purpose/stdout/stderr/truncated/note/error`); on failure
  `error.traceback` and `error.line` identify the model-owned frame.

**`get_scene_info`**
- The `summary` scope is the same object injected as the turn's live summary: it
  is authoritative for present-tense UI state as of capture, and stale once you
  mutate. Re-read, do not remember.
- Use `selection`/`active` to resolve "this"/"it"; `objects` + `filter` for "the
  camera" / "all lights". There is deliberately no full-scene mode; `truncated`
  means narrow the filter, not page.

**`get_rna_info`**
- Use it before writing any `bpy` name you are unsure exists. Existence is
  checked against the cached enumeration; `getattr` lies. `not_found` returns
  close matches.
- `kind`: `type` | `operator` | `search` | `guide`. `operator` includes
  properties, defaults, enum items and `poll_now` — which is this moment's
  context, not a promise.
- `guide` topics are enumerated here (and generated from the guide registry, so
  this list cannot drift): `socket-access`, `context-and-mode`,
  `data-vs-operators`, `units-and-axes`, `read-back`, `version-churn`,
  `no-undo-zone`.

### Surface 3 — the skill asset

**Delivery decision: guides are content in the extension, served by the existing
read-only tool as `get_rna_info(kind="guide", name=...)`.** This **amends
ticket 06's `kind` enum** by adding `guide`; the human must ratify the amendment.
Rejected: a fourth tool (reopens the map's binding "three tools"); a fourth
`get_rna_info` parameter only for docs (worse than a kind); pinning guides into
system prompt (paid every turn); a `Text` datablock the model reads (the curated
namespace withholds `open`, and re-adding it for our own docs widens the boundary
for no gain); no guides at all (the long tail then lands in the system prompt,
which is exactly what this split exists to prevent).

Guides are versioned with `blender_version_min`, retrieved on demand, and static
(the model cannot mutate them).

| guide | content (the long tail, not repeated in the prompt) |
|---|---|
| `socket-access` | named sockets (`node.inputs["Base Color"]`); list at runtime (`[s.name for s in node.inputs]`); why indices are not a contract. Verified 5.2.2 Principled set is `Base Color, Metallic, Roughness, IOR, Alpha, …, Specular IOR Level, …, Transmission Weight, …, Emission Color, Emission Strength, …` — **not** the spelling most training data uses; list, never memorise. |
| `context-and-mode` | poll failure vs silent `CANCELLED`; `temp_override` exists and satisfies `poll()` (verified), but it is a poll-satisfier, not a context switch — `bpy.context.mode` did not change under the override, so verify by read-back, not by `{'FINISHED'}`; when to abandon the operator for the data API. |
| `data-vs-operators` | operator-free equivalents and collection-vs-`bpy.data` link semantics: `collection.objects.unlink(o)` unlinks from the scene while the datablock survives; `bpy.data.objects.remove(o, do_unlink=True)` destroys it. Choose deliberately. Verified `mesh.materials.append(mat)` links slot 0 from zero slots. |
| `units-and-axes` | Z-up, right-handed; `scene.unit_settings` (`METRIC`/`scale_length=1.0`/`METERS` on a factory scene); `scale` vs `dimensions` (world, modifier-inclusive, stale until `view_layer.update()`); local vs world. Rule: use scene units for bare numbers and say so in `purpose`; never silently reinterpret units. |
| `read-back` | printing observable deltas (`dimensions`, `location`, counts, material names) as the evidence line. |
| `version-churn` | how to check a name at runtime; `bpy.types.X.bl_rna.functions`, never `hasattr` on an RNA type; treat any name from memory as unverified. |
| `no-undo-zone` | the detail behind the coverage sentence in ticket 12 §5. |

### The live summary

Reuse ticket 06's `summary` serializer verbatim — one definition, not a second
schema. Inject it as a second `system` message immediately before the user's
turn, **after** the stable system prompt, so the cacheable prefix stays stable
and the volatile part sits at the end. Contents are ticket 06's field list
(`scene, filepath, is_saved, is_dirty, mode, frame, frame_range, render_engine,
unit_system, global_undo, object_count, collection_tree, object_type_counts,
selection{count,names≤8}, active{name,type}`), plus `captured: "turn_start"` and a
schema version.

Authority rules, in the prompt: it wins over memory and over the user's claim of
what is selected; it is a **pre-mutation** snapshot, so it is stale after the
first mutation; `is_saved`/`global_undo` are readable so the model can see
degraded mode and avoid claiming durability the session does not have.

### Deliberately left to tool schemas / runtime RNA, not prose

- All parameter names, types, enums, defaults, include flags, `limit` values —
  JSON Schema, not the prompt.
- The 4,023 type names and 2,498 operator names, and every socket list —
  `get_rna_info` at runtime. A prompt copy would rot and would be trusted over
  the live build.
- The error envelope, truncation flags and caps — schema + the tool result
  describing itself.
- Push cadence, snapshot policy and the full-power approval gate — ticket 12 / 09
  code, not a prose promise the loop might not keep.
- The capability allowlist — generated from the same constant that builds the
  exec namespace, so the prompt cannot describe a restriction that is not
  enforced.

### Evidence

All probes below were run for this ticket against the **installed 5.2.2**
(`--factory-startup -b`, isolated `BLENDER_USER_CONFIG=/tmp/bc-t10-cfg`; no GUI
launched, user config untouched). Facts already established in tickets 02/06/12
are not re-derived.

- `bpy.context.temp_override` exists; inside an override of
  `active_object/object/selected_objects`, `object.mode_set.poll()` flips
  `False → True` and `mode_set(mode='EDIT')` returns `['FINISHED']`, but
  `bpy.context.mode` is still `OBJECT` and stays `OBJECT` after the block. GUI
  behaviour unverified (cannot launch a GUI).
- Poll failure surfaces as `RuntimeError: Operator bpy.ops.object.mode_set.poll()
  Context missing active object`; `object.shade_smooth()` and `object.delete()`
  with nothing selected return `['CANCELLED']` **with no error**.
- `get_rna_type()` on `mesh.primitive_cube_add` still exposes 8 properties; its
  `poll()` is `True` in background. RNA write leaves `bpy.data.is_dirty` `False`.
- `Cube.scale.z *= 2` reads back as `dimensions = (2,2,2)` until
  `view_layer.update()`, then `(2,2,4)` — the concrete read-back trap.
- Factory scene: `unit_settings.system == 'METRIC'`, `scale_length == 1.0`,
  `length_unit == 'METERS'`.
- `mesh.materials.append(mat)` links slot 0 from zero slots;
  `bpy.data.objects.remove(o, do_unlink=True)` removes a linked datablock.
- `bpy.ops.ed.undo_push` exists (and is forbidden to the model).

### Rejected alternatives

- **Everything in the system prompt** — pays the long tail every turn and rots;
  also blurs obligations (prompt) with interface facts (schema).
- **Everything in tool descriptions** — trust rules are cross-tool; repeating
  them on three tools makes them drift out of sync.
- **A fourth tool for guides** — breaks the map's binding "three tools" and adds
  a second read-only doc surface next to `get_rna_info`.
- **Trusting `{'FINISHED'}` / absence of exception as success** — disproved by
  the `CANCELLED` and `temp_override` probes; read-back is the rule.
- **Prose undo promise of per-call revertibility** — contradicts ticket 12; would
  train the model to over-promise in the receipt.
- **A hand-written list of correct API idioms in the prompt** — the 5.2.2 socket
  set alone shows why memory is a bad source; runtime introspection replaces it.

### Human must ratify

1. **The `guide` kind added to `get_rna_info`** (an amendment to ticket 06's
   `kind` enum); if refused, the fallback is a fourth tool, not a fatter prompt.
2. **The corrected undo rationale** — one change per call is for transcript and
   blast-radius legibility, **not** per-call revertibility; Ctrl+Z is per turn.
3. **The `{capability}` line generated from the exec-namespace constant**, i.e.
   that the prompt may never describe a restriction the runtime does not enforce.
4. **The live summary's placement and authority** — injected per turn as a second
   `system` message after the stable prefix, authoritative at capture and stale
   after any mutation.

## Comments
