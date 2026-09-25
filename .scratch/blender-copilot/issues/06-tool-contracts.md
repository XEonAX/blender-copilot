# The three tools' contracts

Type: grilling
Status: resolved
Blocked by: none

## Question

Pin the exact contract for the three tools in the slice, since the tool schema *is* the interface the model codes against.

For each tool — `run_blender_python`, `get_scene_info`, `get_rna_info` — decide:

1. **Input schema.** Exact parameters and their defaults.
2. **Output shape.** What comes back, in what structure, and capped how.
3. **Failure contract.** What the model sees when the call fails.

Specifically:

- **`run_blender_python`**: does the executed code get a *fresh* namespace per call or a *persistent* one across calls? (Persistent lets the model build state up, but makes failures harder to reason about and leaks half-finished objects between calls.) How much of stdout/stderr/traceback is returned — full traceback, or summarised? Where is the truncation cap, and does the model get told it was truncated?
- **`get_scene_info`**: what scope — whole scene, active object, selection, or a named subset — and what does it return for a scene with thousands of objects? What are the include flags and the caps?
- **`get_rna_info`**: what can it actually answer without dumping the API — property lookup on a type, operator signature and poll requirements, enum values, "which operators exist"? It exists to stop the model hallucinating `bpy` APIs; define what it must answer to achieve that.
- Whether a failed call returns control to the model, and whether anything auto-retries (the retry policy itself belongs to *The agent loop's control flow and failure policy*).

Depends on the execution and undo facts in *What exactly happens when we exec model code, and what does undo cover?* — read its answer first.

## Answer

**PROVISIONAL — no human present.** The three schemas below are decided, not
escrowed. Rationale + rejected alternatives are inline; the last line names what
a human must ratify.

Depends on [What exactly happens when we exec model code, and what does undo
cover?](02-code-execution-and-undo.md): the exec namespace is fresh per call and
the tool is **undo-neutral** (it never pushes). Push/snapshot policy is
*What replaces undo as the recovery mechanism?*; retry and round caps are *The
agent loop's control flow and failure policy*. This ticket fixes only the
interface the model codes against.

### Shared conventions (all three tools)

- **Result is always a JSON object serialised to a string**, success or failure.
  OpenAI tool messages are strings; JSON makes truncation flags and error kinds
  machine-readable. Rejected: console-style prose (the model re-parses prose and
  cannot see "you got a subset").
- **Envelope:** `{ok, tool, summary, ...tool fields}` plus, on failure,
  `error: {kind, type?, message, line?, traceback?, hint?}`. `kind` ∈
  `exec_error | tool_argument_error | not_found | invalid_kind`. `summary` is one
  plain-text line for the panel; the model ignores it.
- **One shared 8,000-char cap per result.** Each stream/array is middle-elided
  with a literal `… [N chars elided] …`, `truncated: true`, and a `note` naming
  the narrowing lever. Rationale: ~2k tokens; a round of three results stays
  ~6k. Rejected: 2k (too small for a traceback plus prints), 32k (one object
  list eats the window), uncapped (runaway).
- **A failed call always returns the envelope and control to the model. No tool
auto-retries.** Malformed arguments are a `tool_argument_error` naming the
  missing field, not a loop kill. Retry policy is the loop ticket's.
- **All tool execution is serialised on the main thread in request order**,
  even if the API returns several tool calls in one round. `bpy` is
  main-thread-only and two `exec`s racing would interleave mutations.
- Ordering is deterministic: objects sorted by name, properties in RNA order.

### 1. `run_blender_python`

**Input**

| field | type | notes |
|---|---|---|
| `code` | string, required | `exec`-mode Python source. |
| `purpose` | string, required, 1–80 chars | Imperative one-liner, e.g. `"Move Cube up 1m"`. Used as the panel transcript line and as the undo-step label the loop consumes. |

`purpose` is required, not derived from a leading comment: it forces a
pre-mutation intent statement (the trust rule *What the prompt teaches the model
about Blender* formalises) and produces a usable undo label. Rejected: optional
or first-line-derived — models omit it and derived labels are garbage. The
failure mode (omitted field) is a self-explaining `tool_argument_error`, not a
lost turn.

**Namespace — fresh per call.** A new `types.ModuleType("__main__")`, installed
as `sys.modules["__main__"]` for the duration (the console's `_TempModuleOverride`
pattern) and restored in `finally`. Rejected alternatives and why:

- *Persistent across calls* — undo restores `bpy.data` but never Python state
  ([ticket 02](02-code-execution-and-undo.md)), so a namespace surviving a user
  Ctrl+Z holds freed datablocks and raises `ReferenceError: StructRNA of type
  Object has been removed` on the next call. It also lets a failed call's
  half-built state leak into the next.
- *Persistent within a turn* — same dangling hazard if undo fires mid-turn; adds
  a lifecycle for no capability.
- *Model opt-in (`keep=True`)* — a state model for one line of saved typing.

Prelude names are injected each call: `bpy`, `C = bpy.context`, `D = bpy.data`,
`math`, and `Vector/Matrix/Euler/Quaternion` from `mathutils`. This matches the
Python Console, which is the environment most Blender training text assumes.

**Execution.** `compile(code, "<model>", "exec")` separately (so `SyntaxError`
carries `lineno`/`offset`), then `exec` inside
`redirect_stdout` + `redirect_stderr` + a console-style stdin redirect to EOF
(`contextlib.redirect_stdin` does not exist — verified) so `input()` cannot lock
the GUI; `except BaseException` (catches `SystemExit`, verified) and
`traceback.format_exception`.

**Output**

```json
{"ok": true, "status": "ok|error", "purpose": "Move Cube up 1m",
 "stdout": "...", "stderr": "...", "truncated": false, "note": null,
 "error": null}
```

- On error: `error = {type, message, line, traceback}`, `ok: false`. The **full
traceback is returned** — it is the only thing that lets the model fix its own
bug, and it is cheap. `line` is extracted by walking `e.__traceback__` to the
frame whose `co_filename == "<model>"` (verified: `e.__traceback__.tb_lineno`
alone gives the *outer* exec call site, which is wrong). `SyntaxError` uses
`e.lineno`.
- Caps: **stdout 4,000 / stderr 2,000 / traceback 2,000 chars**, middle-elided
  (head+tail for stdout — setup prints at the head, errors at the tail;
  tail-priority for the traceback). `truncated: true` and a `note` always
  accompany elision.
- Bpy report text appears in stderr *and* inside the `RuntimeError` message.
  Accept the duplication; do not string-dedupe.

**Documented non-guarantees** (tool description, not enforcement):

- Synchronous on the main thread and **not cancellable or time-boundable** — a
  `while True:` freezes Blender (ticket 02's research, "could not determine" #8).
- **No sandbox.** `subprocess`, filesystem and network are not filtered; a regex
  filter is bypassable and buys false assurance. What protects the machine is the
  untrusted-code warning and the transcript, not the tool.
- **`{'CANCELLED'}` with no report is a silent no-op** and the tool cannot detect
  it generically (ticket 02, §2). The tool description tells the model to check
  the operator's return value. Rejected for v1: AST-rewriting the model's code to
  capture every operator result — fragile and semantics-changing; revisit only if
  silent no-ops become an observed failure.
- **The tool never calls `bpy.ops.ed.undo_push`** and makes no per-call
  revertability claim. "One meaningful change per call" is prompt guidance for
  readable transcripts, not an undo contract.

### 2. `get_scene_info`

**Input**

| field | type | default |
|---|---|---|
| `scope` | `"summary" \| "selection" \| "active" \| "objects"` | `"summary"` |
| `filter` | `{types?: [enum], name_contains?: string, collection?: string, selected_only?: bool}` (only for `objects`) | `{}` |
| `include` | array of `"transform" \| "dimensions" \| "parent" \| "collections" \| "materials" \| "modifiers" \| "constraints" \| "custom_properties" \| "mesh_stats"` | per scope |
| `limit` | int, 1–100 | 25 |

Include defaults: `summary` → none (aggregate only); `selection`/`objects` →
`["transform"]`; `active` →
`["transform","dimensions","materials","modifiers","mesh_stats"]`.

**Scope rationale.** Whole-scene dump is banned by the map. Active-only is too
narrow for "make this thicker", which needs candidates. Selection-only cannot
answer "what is in my file". So: a `summary` default plus three narrower scopes,
and **`summary` always carries `active` and `selection` names** because resolving
an ambiguous target is the highest-frequency first call.

**Output**

- `summary`: `{scene, filepath, is_saved, is_dirty, mode, frame, frame_range,
  render_engine, unit_system, global_undo, object_count, collection_tree,
  object_type_counts, selection:{count,names≤8}, active:{name,type}}` — counts,
  never a list.
- `selection`/`objects`: `{matched, returned, truncated, objects:[...]}` with the
  requested `include` fields per object.
- `active`: one object at the deep include set.

**Thousands of objects.** The guarantee is **counts + a bounded list + an
explicit `truncated` flag + a stated way to narrow**, never an attempted full
enumeration. `object_type_counts` in `summary` conveys the scene's shape without
listing it, so the model can pick a filter instead of paging. Every list result
carries `note: "87 matched, 25 returned; narrow with filter.name_contains or
types"`.

Caps: `limit` 25/100 as above; the shared 8,000-char cap drops trailing entries
and reports; `mesh_stats` is computed only for returned objects (it evaluates the
mesh) and is documented as the expensive flag; `custom_properties` ≤10 keys,
values stringified and truncated at 200 chars; name lists ≤20 with a count.

Rejected: an arbitrary filter/query DSL — more surface for the model to guess
wrong, and the three filters cover "the thing named X", "all cameras" and "what
is selected".

### 3. `get_rna_info`

Not a documentation dump — the minimum that stops `bpy` hallucination:

1. **Does it exist?** type, attribute, or operator id.
2. **Type properties**: identifier, type, array length, read-only, default,
   description, enum items, numeric range/subtype, with a `filter` so `Object`'s
   141 properties don't arrive whole.
3. **Type methods**: `bl_rna.functions` with parameters (49 on `Object`).
4. **Operator signature**: properties + defaults + enum items + description via
   `bpy.ops.<id>.get_rna_type()`.
5. **Enum values**: from `enum_items` on either kind of property.
6. **Pollability now**: `bpy.ops.<id>.poll()` as a bool, explicitly labelled
   context-dependent.
7. **Which operators exist**: substring search over a cached full enumeration.

**Input**

| field | type | notes |
|---|---|---|
| `kind` | `"type" \| "operator" \| "search"`, required | Explicit, not sniffed from the name. |
| `name` | string, required | Type name / operator id / search substring. |
| `filter` | string? | Substring filter on property and function names. |
| `limit` | int, 1–200 | default 40 (properties), 50 (search). |

**Output**

- `type`: `{found, id, name, description, base, property_count, returned,
  truncated, properties:[{id,type,array_length,readonly,default,enum_items?,
  enum_count?,range?,subtype?,description?}], functions:[{id,parameters,description}]}`.
  `base` is the base-chain walked via `.base` (`Object.bl_rna.base` is a single
  struct, not a list — verified).
- `operator`: `{found, id, name, description, poll_now: bool,
  poll_note, properties:[...]}`.
- `search`: `{pattern, total_matches, returned, truncated, operators:[ids]}`.

**Facts verified on the installed 5.2.2** (headless probes, `--factory-startup
-b`): `get_rna_type()` exposes 8 properties for
`mesh.primitive_cube_add` with defaults and enum items
(`align` → `WORLD/VIEW/CURSOR`); `poll()` returns `True` in background and
`False` for `console.scrollback_append`, so context failure *is* introspectable;
`bpy.types.Object.bl_rna.properties` = 141 and `bl_rna.functions` = 49;
`bpy.types` holds 4,023 names (**never list them** — exact-name lookup only);
all **2,498** operators enumerate in **0.035 s** by walking `dir(bpy.ops)` plus
one nested level (`ed.*`). Two traps: `bpy.types.MESH_OT_*` is not reachable, so
`bl_options` is **not** in the contract; and `getattr(bpy.ops.mesh,'frobnicate')`
returns a function rather than raising, so existence is checked against the
cached enumeration, not `getattr`. Per house rules, use `bl_rna.functions`, never
`hasattr` on an RNA type.

**Not-found is the anti-hallucination feature.** `not_found` returns 5–10 close
matches from the enumeration and, for a wrong-`kind` call, a hint naming the
right kind. Caps: `limit` 40/200; enum items ≤30 with `enum_count` always
present; search ≤50; shared 8,000-char cap. This tool only reads RNA, never runs
lifecycle code, and is therefore free and safe.

### What is deliberately not fixed here

Prose guidance — socket-vs-index access, units/axes, `temp_override`,
inspect-before-mutate, tone — lives in the tool *descriptions* and system prompt
and is *What the prompt teaches the model about Blender*'s to write. Push
cadence, snapshot cadence and the user-facing undo promise are *What replaces
undo as the recovery mechanism?*'s. Retry counts, round caps and "does a failed
call hand back to the user" are *The agent loop's control flow and failure
policy*'s.

### Human must ratify

1. **`purpose` required on `run_blender_python`** — the only schema field added
   purely for trust/undo labelling; it can be dropped to optional with no other
   contract change.
2. **Fresh namespace per call** (not persistent) — the dangling-after-undo
   argument is strong but this is the choice most likely to annoy a user who
   wanted console-like state.
3. **The 8,000-char cap and its 4,000/2,000/2,000 split.**
4. **`get_scene_info` scope enum and the 25/100 limit**, i.e. that there is no
   "just give me everything" mode by design.

## Comments
