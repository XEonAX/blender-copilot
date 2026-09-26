# 03: The read-only tools

**What to build:** the other two thirds of the tool surface, so the model can look
before it acts instead of guessing. Ask "what's in my scene?" and get a real answer
without the panel dumping the scene into the model's context; ask "what does this
operator take?" and get it from the live build rather than from anything
hand-written.

**Blocked by:** 02 (One tool call, end to end)

**Status:** resolved
**Triage:** ready-for-agent

- [x] "What's in my scene?" is answered from a bounded summary — counts, plus a capped list with a truncation flag — never a full dump.
- [x] Asking for more than the cap returns the cap and says it was truncated, rather than silently shrinking.
- [x] "What arguments does this take?" is answered by exact-name lookup against the live build; an unknown name reports not-found rather than inventing an answer.
- [x] The long-tail guidance kind exists, so idiom questions do not require a fourth tool.
- [x] Both tools respect the shared result-size cap, and a truncated result says so.
- [x] The prompt no longer needs to shy away from naming the tools it now actually has.

**Context:** *The three tools' contracts* fixes the envelope and the caps;
*What the prompt teaches the model about Blender* fixes where guidance lives and
why runtime introspection beats a hand-written API list. Note that the
`{capability}` line from the latter is **moot** — the capability restriction was
rejected — so the prompt must not describe a restriction the runtime does not
enforce.

## Answer

> ### Retry note: the rollback is repaired, and this Answer was re-measured
>
> The first attempt hit a mid-session tree rewrite by another writer: six files
> were reverted to `0225b64` (the pre-ticket-01, *pager* revision) and the ticket
> files for 01 and 02 came back with their answers stripped. **The orchestrator
> repaired that**, and this retry re-ran every gate on the repaired base. The
> rolled-back warning that used to be here is replaced by this note; the
> orchestrator's own `Verification failed` record is left at the bottom of this
> file as the reason the retry exists, and its two lost wording deltas plus its
> one stale-assertion fix were re-applied in this session.
>
> The base this evidence was measured on, read (not assumed) in the retrying
> session:
>
> ```
> $ git --no-pager log --oneline -3
> 17f4195 (HEAD -> main) Make one tool call work end to end (build ticket 02)
> afcebaa Delete the dead pager (build ticket 01)
> 0225b64 Drop the screen rail, allow delegation, and make the orchestrator a skill
> ```
>
> `conversation.py` has the loop and no pager, and `panel.py` / `stream.py` /
> `tests/test_conversation.py` / `tools/panel_draw_smoke.py` are `17f4195`'s. All
> evidence in the retry section is from the retrying session, **in-tree**. The
> earlier `/tmp/t03-tree` verification copy is gone and is no longer cited.
>
> **Retry outcome.** Three edits: the two wording deltas the rollback lost
> (`__init__.py`'s module docstring, `conversation.py`'s `_demo()` assistant line)
> re-applied, and judgement call 8's two-line fix applied to ticket 02's
> `tests/test_conversation.py`. Then every gate was re-run in-tree, and all five
> pass: `test_conversation.py` **exit 0 / 167 checks**; `test_read_only_tools.py`
> **exit 0 / 110 checks**; `panel_draw_smoke.py` **`SMOKE OK`**;
> `read_only_tools_probe.py` **108 checks / `SMOKE OK`**; and ticket 02's windowed
> `loop_panel_probe.py` **`SMOKE OK`** with a screenshot — the probe the first
> attempt could not run at all. No live request was made: AGENTS.md reserves real
> spend to ticket 16 and nothing in this ticket needs it, so the transport smoke
> was deliberately not run.

### Decisions this transcribes (ratified; not re-decided)

Found by searching `.scratch/blender-copilot/issues/` for the two titles the ticket
names:

- **`06-tool-contracts.md`** — the envelope `{ok, tool, summary, …}` plus
  `error:{kind,…}` with `kind ∈ exec_error | tool_argument_error | not_found |
  invalid_kind`; the **one shared 8,000-char cap**; a JSON object serialised to a
  string on every call; `get_scene_info`'s four scopes, its include set and
  `limit` 25/100; `get_rna_info`'s exact-name lookup with `not_found` returning
  5–10 close matches; `limit` 40 properties / 50 search / 200 max; enum items ≤30
  with `enum_count` always present; exact-name lookup against a *cached*
  enumeration because `getattr` lies.
- **`10-system-prompt-and-blender-idioms.md`** — the four placement surfaces
  (system prompt / tool descriptions / runtime RNA / guides), the corrected
  "one change per call" rationale (legibility and blast radius, **not** per-call
  revertibility), the live summary as the `summary` scope serialised with a
  `captured` stamp and a schema version, and the `guide` kind with its seven
  topics.

Neither turned out wrong. Two things in ticket 10 are **not** in the shipped
prompt, and that is a deliberate, visible departure rather than a quiet one:

- its `{capability}` line is gone — the map says the restriction it would have
  described was rejected, so the prompt has no restriction to declare.
- the sentences "the loop pushes that step at the end of the turn" and "The loop
  also diffs the scene before and after the turn, and its receipt will contradict
  a false claim" are **not** written, because both assert mechanisms that **build
  ticket 05 owns and that do not exist yet**. A prompt that promises an enforcement
  it does not have is the same failure the `{capability}` line was removed for. The
  *obligations* those sentences existed to enforce survive verbatim: never calling
  the undo operators yourself, and reading a change back before claiming it.
  Build ticket 05 should restore both sentences when it lands the push and the
  receipt; the exact text is in ticket 10.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/execution.py` | **+1100 lines, bpy-free.** The two tools' schemas; the readers `scene_info` / `rna_info` / `scene_summary`; validation, filters, the caps, close matches, and `_fit_by_dropping` (see judgement call 3). Dispatch now routes all three tools. The sandbox is untouched. |
| `blender_copilot/toolbox.py` | **+160 lines.** `LiveWorld`: the prelude plus the whole `world` the readers read through — scene facts, objects (after a depsgraph update), active/selection, the collection tree, and the two cached name enumerations with `rna_of_type` / `rna_of_operator` / `operator_poll`. `execute()` passes `WORLD`. |
| `blender_copilot/guides.py` | **new, 224 lines.** The seven guides, each with a one-line `summary` (what the description and the not-found matches show) and a `body` (what a retrieval returns), plus `TOPICS` and `topic_list()`. No imports at all, so it loads from anywhere. |
| `blender_copilot/prompt.py` | The base prompt is now ticket 10's, delivered in full: three tools, the live summary's authority, target resolution, change discipline, read-back, API discipline, capability and style. `live_summary()` returns **`execution.scene_summary(toolbox.WORLD)` serialised** — the same object `get_scene_info(scope="summary")` returns, so there is one definition of the summary and not two. |
| `tests/test_read_only_tools.py` | **new, 526 lines, 110 checks** — see judgement call 7 for why it is not a section of `tests/test_conversation.py`. |
| `tools/read_only_tools_probe.py` | **new, 411 lines, 108 checks.** Real `bpy`, installed 5.2.2, `--background --factory-startup`, verdict token. |
| `blender_copilot/__init__.py` | docstring: "No tools" → the three tools exist and run. |
| `blender_copilot/conversation.py` | one line of the **demo transcript**: it said "this prototype has no tools", which this ticket makes false. |
| `tools/loop_panel_probe.py` | one check: the trailing summary is now parsed as JSON with `captured == "turn_start"` rather than matched against the old `"Live scene:"` prefix. |
| `tests/test_conversation.py` | **ticket 02's file, edited by this ticket** — its "unknown tool" example called `get_scene_info`, which this ticket makes a *real* tool, so the call stopped being rejected and the suite failed. Swapped to `frobnicate`, a name no tool has. Judgement call 8. |
| `blender_copilot/__init__.py`, `blender_copilot/conversation.py` | the two wording deltas above, re-applied in the retry after the rollback lost them. |

### Evidence — first attempt (superseded by the retry below)

Every command below was run in the first session. `logs/` is gitignored; the raw
outputs are saved there. **Read this as the first attempt's record only:** the
runs it labels *in-tree* were taken against a tree another writer had rolled back
under it, and the retry section further down re-ran every gate in-tree on the
repaired base. Where the two disagree, the retry is the measurement.

**1. This ticket's checks, in-tree — 110 checks, exit 0.**

```
$ cd /Users/user/Projects/blender.anx.copilot && python3 tests/test_read_only_tools.py
-- get_scene_info --
ok   a scene summary is ok
ok   it counts the objects
ok   it counts them by type
ok   it names the active object and its type
ok   it reports the selection count and names
ok   it carries the scene facts
ok   and the degraded-mode fact
ok   the collection tree is nested, with counts
ok   the summary scope of the field list is exactly ticket 06's
ok   a summary is a summary: it lists nothing, whatever the scene holds
ok   the summary says when it was captured
…
ok   a big scene is capped at the default
ok   the note names the narrowing lever, not just the number
ok   asking past the cap returns the cap
ok   and says the cap was applied
ok   a result that would blow the shared cap drops entries instead
ok   the wire content respects the cap
ok   and it says which list was cut
ok   an unknown operator is not found
ok   and it names close matches instead of inventing an answer
ok   every match is a name that really exists
ok   guide socket-access is served
… (one per topic)
ok   the description's topic list is generated from the registry, so it cannot drift
ok   the three tools are declared, in the order the model reads them

all read-only tool checks passed
```

Whole run: `logs/read-only-tools-checks.txt`, 110 `ok`, 0 `FAILED`.

**2. Ticket 02's suite, against this ticket's code — 167 checks, exit 0, in the
repaired copy.** This is the integration check that matters most: the two new
tools are declared on every round the loop already sends, and adding them breaks
nothing ticket 02 measured.

```
$ cd /tmp/t03-tree && python3 tests/test_conversation.py
…
all checks passed
$ python3 tests/test_conversation.py | grep -c '^ok '
167
```

167 is ticket 02's own count (it was 167 when it landed), and the *one* stale
assertion it needed is judgement call 8. Whole run:
`logs/ticket03-suite-copy.txt`.

**3. The read-only tools against the installed 5.2.2 — 108 checks, 0 failed,
`SMOKE OK`.**

```
$ cd /tmp/t03-tree && python3 tools/bounded_run.py 90 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/read_only_tools_probe.py
-- the registry and the prompt --
ok   the addon declares three tools
ok   the declared names are the three the prompt teaches
ok   the base prompt names all three tools, so it no longer shies away from them
ok   and it asserts no capability restriction the runtime does not enforce
-- get_scene_info, scope=summary --
ok   a summary over the real factory scene is ok
     scene='Scene' engine='BLENDER_EEVEE' mode='OBJECT' unit='METRIC' undo=True
ok   it counts the objects
ok   and counts them by type rather than listing them
ok   the factory scene is the cube, the camera and the light
ok   a summary lists no objects, so no scene size can make it grow
ok   the turn's live summary is the very same object, not a second schema
ok   the live summary is what a turn actually sends, as the trailing system message
-- the caps, against a scene that has grown --
ok   and says it was truncated, rather than shrinking silently
     after adding 40 empty objects: matched=43 returned=25
ok   a scene past the cap returns the cap
ok   and the true count
ok   and it is the first N of that order, not an arbitrary N
-- what an object entry actually carries --
     cube: dimensions=[2.0, 2.0, 2.0] mesh_stats={'vertices': 8, 'edges': 12, 'polygons': 6, 'material_slots': 1}
ok   a read after an un-updated change is NOT stale (2.0 -> 4.0)
ok   a collection filter links through users_collection (Scene Collection)
ok   a collection filter links through users_collection (Collection)
-- get_rna_info, kind=type --
     Object: properties=141 returned=10 base='ID' functions=49
ok   the property count is ticket 06's measured 141 (never listed whole)
ok   the base chain is walked via .base
ok   functions come from bl_rna, not `hasattr`
-- get_rna_info, kind=operator --
     mesh.primitive_cube_add: properties=8 poll=True
ok   poll_now is this moment's context, and True in background
ok   a context-dependent poll really reports False
ok   `getattr` lies about operators, which is why existence is not asked of it
     enumerated 2498 operators, 4023 type names
-- get_rna_info, not found --
     type 'Obj' -> ['Object', 'ObjectBase', 'ObjectDisplay', 'ObjectLineArt', 'ObjectShaderFx', 'OBJECT_OT_align', 'ObjectModifiers', 'OBJECT_PT_display']
ok   a truncated type name gets type matches, shortest first
ok   and the wrong-kind hint names the right kind
-- get_rna_info, search --
     search primitive_cube_add: 2 matches, first=['mesh.primitive_cube_add', 'mesh.primitive_cube_add_gizmo']
-- get_rna_info, guide --
ok   guide socket-access is served from the shipped registry
ok   guide no-undo-zone is served from the shipped registry
-- the error kinds --
ok   a wrong kind is invalid_kind, not not_found
ok   an unknown tool names the three that exist
-- a read-only call through the real loop --
ok   the loop ran the read-only call
ok   and the model receives the summary envelope
ok   with the tool's own name on it
ok   so the next round can go out
ok   carrying the read result against the call's id

all read-only tool checks passed
SMOKE OK
BOUNDED | finished on its own in 1.5s | rc=0
```

Whole run: `logs/read-only-tools.txt`, 108 `ok`, 0 `FAILED`, one `SMOKE OK`.

The counts that matter, re-measured on the installed build rather than recalled:
**141** properties on `Object`, **49** `bl_rna.functions`, **8** properties on
`mesh.primitive_cube_add` with `poll()` True in background, **2,498** operator ids
and **4,023** type names enumerated. Ticket 06's numbers hold.

**4. The panel draw check, in-tree — `SMOKE OK` once, `SMOKE FAILED` zero times.**
This runs against the *rolled-back* `panel.py`, so it says nothing about this
ticket's code; it is here because the ticket names it as a gate.

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
all draw bodies ran
SMOKE OK
Blender quit
BOUNDED | finished on its own in 1.3s | rc=0
```

**5. The gate that cannot run in-tree, and why.**

```
$ python3 tests/test_conversation.py
Traceback (most recent call last):
  File "…/tests/test_conversation.py", line 182, in <module>
    check("no page-size constant", not hasattr(conversation, "PAGE_LINES"))
AssertionError: FAILED: no page-size constant
EXIT=1
```

That is ticket 01's check failing against ticket 01's rolled-back
`conversation.py`. It is the tree, not this ticket; command 2 above is the same
suite in the same order once the six files are back at `17f4195`.

### Evidence — retry session, 2026-09-26, in-tree

Every command below was run in the retrying session, after the three re-applied
edits and before this Answer was written. Raw outputs: `logs/ticket03-retry-*.txt`.

**1. `python3 tests/test_conversation.py` — exit 0, 167 checks, 0 failed.** This is
the gate the orchestrator reported failing. It passes now both because the base is
repaired and because ticket 02's stale "unknown tool" example is fixed.

```
$ python3 tests/test_conversation.py; echo "RC=$?"
…
ok   the context hook is asked once per request
ok   so the trailing summary is the one captured for THAT request
ok   and the base prompt is byte-identical

all checks passed
RC=0
$ grep -c '^ok '  …   # 167
$ grep -c 'FAILED' …  # 0
```

**2. `python3 tests/test_read_only_tools.py` — exit 0, 110 checks, 0 failed.**

```
$ python3 tests/test_read_only_tools.py; echo "RC=$?"
ok   an unknown tool is named, and the message names the three that exist

all read-only tool checks passed
RC=0
$ grep -c '^ok '  …   # 110
$ grep -c 'FAILED' …  # 0
```

**3. The panel draw smoke, gated on the token — `SMOKE OK` once, `SMOKE FAILED`
zero.** Blender exits 0 either way, so this is gated on the token and not the
status.

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
…
all draw bodies ran
SMOKE OK
Blender 5.2.2 LTS (hash d13f752e3b9c built 2026-09-15 01:49:19)

Blender quit
BOUNDED | finished on its own in 1.2s | rc=0
```

**4. The read-only tools against the installed 5.2.2 — 108 checks, 0 failed,
`SMOKE OK`.**

```
$ python3 tools/bounded_run.py 90 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/read_only_tools_probe.py
…
all read-only tool checks passed
SMOKE OK
BOUNDED | finished on its own in 0.8s | rc=0
$ grep -c 'SMOKE OK'     …  # 1
$ grep -c 'SMOKE FAILED' …  # 0
$ grep -c 'FAILED'       …  # 0
```

**5. Ticket 02's windowed loop probe — `SMOKE OK`, plus a screenshot.** The first
attempt could not run this (the tree was rolled back under it). It runs now, and
it is the check that covers this ticket's edit to `loop_panel_probe.py`: the
trailing summary is parsed as JSON and its `captured` stamp must be `turn_start`.

```
$ python3 tools/bounded_run.py 120 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --python tools/loop_panel_probe.py
…
LOOP | ok   the live scene summary is still the last message
LOOP | ok   every call got exactly one row, in order
LOOP | screenshot: {'FINISHED'} -> loop-panel.png, sidebar tab 'Copilot'
SMOKE OK
BOUNDED | finished on its own in 2.6s | rc=0
```

The screenshot (`logs/ticket03-retry-loop-panel.png`) shows the `Copilot` sidebar
tab mid-turn: the user turn, the assistant prose, the `Scale Cube 1.5x on Z` tool
row with its `output` expander, the `-> full code in "Copilot Code"` identity row,
and the errored `Try a bigger scale` row drawn in red. It does **not** show the
live summary or the prompt — both are wire content, neither is a draw body.

**6. The final sweep, chained with `&&` and gated on the token.** Blender exits 0
even when a `--python` script raises, so the chain gates on `grep -q 'SMOKE OK'`
and refuses to reach the final line if a token is missing:

```
$ python3 tests/test_conversation.py > /tmp/f1.txt 2>&1 \
  && python3 tests/test_read_only_tools.py > /tmp/f2.txt 2>&1 \
  && python3 tools/bounded_run.py 60 -- … --python tools/panel_draw_smoke.py > /tmp/f3.txt 2>&1 \
  && python3 tools/bounded_run.py 90 -- … --python tools/read_only_tools_probe.py > /tmp/f4.txt 2>&1 \
  && ! grep -q 'SMOKE FAILED' /tmp/f3.txt /tmp/f4.txt \
  && grep -q 'SMOKE OK' /tmp/f3.txt && grep -q 'SMOKE OK' /tmp/f4.txt \
  && echo "FINAL: all four gates green | conv=$(grep -c '^ok ' /tmp/f1.txt) | …"
FINAL: all four gates green | conv=167 | ro=110 | probe=108
```

### Judgement calls the orchestrator should look at

1. **An out-of-range `limit` is clamped, not refused** — `limit: 500` returns the
   100 maximum and *says so in the note*. Ticket 06 says `limit` is 1–100 and this
   ticket says "asking for more than the cap returns the cap and says it was
   truncated"; an argument error would have refused the request instead of
   answering it. A non-numeric limit falls back to the default with a note rather
   than an error, for the same reason. An unknown *scope* or *include flag* is
   still an argument error, because those are semantics rather than budgets.
2. **`note` means "something was withheld", and `truncated` says which kind.**
   A list cut by `limit` gets `"87 matched, 25 returned; narrow with
   filter.name_contains or types"` (ticket 06's own sentence); a list narrowed by
   a *filter* is **not** truncation — the model asked for fewer — so it carries
   the count and no truncation claim. A run that fits gets `note: null`, because
   `matched`/`returned` already say the same thing in the envelope.
3. **The shared cap drops entries for the read-only tools instead of eliding
   characters.** This is a real behaviour change from `run_blender_python`, and it
   was forced by a measurement: the `Object` type answer *exceeds 8,000 characters
   on the installed build* (49 functions each carrying a description), and the
   sandbox's existing fallback — elbow the JSON string — leaves the model with a
   document it cannot parse. Ticket 06 already asks for this ("the shared
   8,000-char cap **drops trailing entries** and reports"), so the reader halves
   the longest list until the answer fits, corrects `returned`, and says in the
   note which lists were cut: `over 8000 chars, so lists were cut to: properties
   20, functions 20, properties 10`. `run_blender_python` keeps the string
   elision, because a traceback has no entries to drop.
4. **Close matches are ranked shortest-first inside each tier.** Scrolling off a
   live measurement: asking for the type `Obj` against the real build returned
   eight `OBJECT_MT_light_linking_context_menu`-shaped ids before `Object`,
   because those sort earlier and the menu names are six times longer. The fix
   (`Object` first, then `ObjectBase`, …) is checked in both suites. Nothing is
   invented: every match exists in the enumeration.
5. **Two fields beyond ticket 06's list, both caps that must be visible.**
   `collection_tree_truncated` (the summary's tree is bounded at 30 nodes, and a
   cut tree must not read as a small one) and `custom_property_count` (the dict is
   capped at 10 keys). Both are always present when the relevant field is.
6. **`poll_now` can be `null`.** Ticket 06 says `bool`; if `poll()` itself raises
   the reader reports `null` and `poll_note` explains, rather than inventing
   `false`, which is a different claim.
7. **This ticket's CPython checks live in their own file.** They belong in
   `tests/test_conversation.py` beside the loop's, and that is where they started;
   the concurrent rollback deleted them with ticket 02's checks before I could
   finish. `tests/test_read_only_tools.py` needs nothing from `conversation.py`,
   so it survives that. **They should be merged back when the tree is coherent** —
   the intent is one suite, and the file says so at the top.
8. **`tests/test_conversation.py` contained one assertion this ticket made
   false, and the retry applied the fix in-tree.** Ticket 02 used `get_scene_info`
   as its example of an *unknown* tool; this ticket makes it a real one, so the
   call stopped being rejected. Two strings, applied:

   ```
   -        {"id": "a2", "function": {"name": "get_scene_info", "arguments": "{}"}}, {}
   +        {"id": "a2", "function": {"name": "frobnicate", "arguments": "{}"}}, {}
   -        and "get_scene_info" in unknown["envelope"]["error"]["message"],
   +        and "frobnicate" in unknown["envelope"]["error"]["message"],
   ```

   **This is a deliberate edit to a file ticket 02 owns, and it is reported as
   such.** The alternative was to leave a red gate on a base the orchestrator had
   already declared repaired. `frobnicate` is a name no tool has; the suite's own
   assertion demands the message name the bad one, and
   `read_only_tools_probe.py` separately asserts that the unknown-tool message
   names all three tools that *do* exist — so the two checks together still pin
   the behaviour. Worth flagging to the ticket-02 executor: there are three tools
   now, so its "unknown tool" example has to be something that is not one of them.
9. **The live summary changed shape**, from a hand-written sentence
   (`Live scene: 3 objects, mode OBJECT, …`) to the `summary` scope's JSON. This is
   ticket 10's "reuse ticket 06's serializer verbatim — one definition, not a
   second schema" applied: the prompt tells the model the summary *is* the
   `get_scene_info` summary, so it had better be. It costs the model nothing and
   gains it `frame`, `is_dirty`, `global_undo`, `object_type_counts` and the
   collection tree. `tools/loop_panel_probe.py`'s check for it was updated to
   parse JSON.

### What I could not verify

- **No request reached DeepSeek.** The orchestrator's note reserves real spend to
  ticket 16 and says not to run the transport smoke, so I did not, and no key was
  read, printed or logged. Everything about the provider is therefore ticket 16's
  measurement, reused.
- **The live model's own choice to call these tools is untested.** The loop test
  in the probe drives a *scripted* call, so what is proven is that a read-only
  call survives the loop end to end; that the model will ask for one is a
  different claim that only a live send can settle.
- **The panel was not photographed on the first attempt; the retry photographed
  it.** `tools/loop_panel_probe.py` is ticket 02's windowed probe and it needs a
  GUI run plus a coherent `panel.py`/`conversation.py`. It ran in the retry and
  passed with `SMOKE OK`, saving `logs/loop-panel.png` (copy:
  `logs/ticket03-retry-loop-panel.png`). That picture shows the tool rows, code
  rows and the errored row drawing — it does **not** show the new prompt string or
  the new summary, because neither is a draw body: the prompt is wire content and
  the summary is a `system` message. So "the three tools are named in the prompt"
  rests on `read_only_tools_probe.py`'s assertion, not on the picture.
- **The two prompt sentences ticket 05 owns** are, until 05 lands, absent (see the
  decisions section). Nothing about the undo push or the receipt is asserted to
  the model right now.

### Human must ratify / do

1. ~~Restore the six rolled-back files~~ — **done by the orchestrator** before this
   retry. `git status` shows no rollback: the only modified files are this ticket's
   own (`execution.py`, `prompt.py`, `toolbox.py`, `tools/loop_panel_probe.py`), the
   two re-applied wording deltas (`__init__.py`, `conversation.py`), ticket 02's
   two-line fix, and (untracked) this ticket's three new files.
2. ~~Apply judgement call 8's two-line fix~~ — **done in-tree in the retry**; see
   the row in "what changed" and judgement call 8. It is an edit to a file ticket
   02 owns, and it is reported here rather than buried.
3. The ticket-10 departures in the decisions section: confirm that leaving the
   push/receipt sentences out until ticket 05 lands is what you want, or have 05
   restore ticket 10's sentence verbatim.

## Verification failed — orchestrator, 2026-09-26

**Its human-must-do item 1 is done.** The orchestrator restored the six rolled-back
files and the ticket files for 01 and 02 from `17f4195` before this check, so the
base is now the one this ticket was written against: `conversation.py` has the loop
back (7 hits for `pump`/`attach`/`PHASE_TOOL`/`_cap_reason`), the pager is still
gone, and tickets 01 and 02 read `resolved` again.

On that repaired base the orchestrator's own gate fails:

```
$ python3 tests/test_conversation.py; echo "RC=$?"
Traceback (most recent call last):
  File "…/tests/test_conversation.py", line 340, in <module>
    check(
  File "…/tests/test_conversation.py", line 39, in check
    assert condition, f"FAILED: {label}"
AssertionError: FAILED: an unknown tool is named and runs nothing
RC=1
```

**The failure is not in this ticket's tools.** `python3 tests/test_read_only_tools.py`
passes on the same base — exit 0, 110 checks, `all read-only tool checks passed`.
The one failing check is ticket 02's, at `tests/test_conversation.py:337`: it calls
`get_scene_info` to prove an *unknown* tool is rejected. `get_scene_info` was
unknown when ticket 02 wrote that line, and this ticket implements it, so the call
now succeeds and the assertion fires. The fix is the two-line one this ticket
already named in its judgement call 8: use a name no tool has.

**Also lost to the rollback**, because they lived in files that were reverted: this
ticket's wording deltas to `conversation.py` (the `_demo()` assistant line that no
longer claims the prototype has no tools) and to `__init__.py`'s module docstring.
The `tools/loop_panel_probe.py` edit survived, as did every new file.

Retrying once, with this note. The retry must not re-do the repair.

**Outcome of the retry: passed, and the orchestrator re-ran every gate itself.**

| gate | orchestrator's own run |
|---|---|
| `python3 tests/test_conversation.py` | exit 0, 167 checks, 0 failed |
| `python3 tests/test_read_only_tools.py` | exit 0, 110 checks, 0 failed |
| bounded draw smoke | `SMOKE OK` ×1, `SMOKE FAILED` ×0, `BOUNDED \| finished on its own in 0.8s` |
| bounded `read_only_tools_probe.py` | 108 checks, 0 `FAILED`, `SMOKE OK` ×1 |
| windowed `loop_panel_probe.py` | `SMOKE OK` ×1, 0 `FAILED`, `dimensions.z=3.000`, fresh screenshot |

The probe's measured numbers were reproduced independently rather than read off
the answer: `Object` properties=141, functions=49, `mesh.primitive_cube_add`
properties=8 with `poll=True`, 2498 operators enumerated, 4023 type names, and all
seven `guide` topics served. `git diff --numstat HEAD` shows this ticket's work
and nothing else — no rollback survives in the tree.

