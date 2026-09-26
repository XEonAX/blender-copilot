# 02: One tool call, end to end

**What to build:** the thing the whole effort exists for. Type "make the cube
taller" into the panel and have it actually happen: the model asks to run code, the
code executes inside Blender with the live scene, the panel shows the call and its
output, and the model's closing reply arrives. This is the tracer bullet — it cuts
the loop, the tool surface, the worker and the panel in one piece.

**Blocked by:** 01 (Remove the dead paging code)

**Status:** resolved
**Triage:** ready-for-agent

- [x] With a cube in the default scene, asking for it to be made taller changes the cube, with no hand-written code.
- [x] The panel shows a tool row naming what ran, with its output behind the expander.
- [x] Code runs with a fresh namespace per call: a name defined in one call is not visible to the next.
- [x] A failing call returns the full traceback, and the model receives it on the following round.
- [x] At most one tool call executes per timer tick, so the panel keeps repainting while a turn is in flight.
- [x] The turn stops at the round and call caps and *reports* what it did rather than unwinding it.
- [x] Nothing retries automatically, and a malformed call is fed back to the model as a continuation rather than a retry.
- [x] Stop mid-turn leaves a history the provider will accept, so a turn can always be sent again.

**Context:** the loop's control flow and failure policy were decided in *The agent
loop's control flow and failure policy*; the tool's envelope, its fresh-namespace
rule and its always-return-the-traceback rule in *The three tools' contracts*.
Follow those rather than re-deciding them.

## Answer

### Decisions this transcribes (ratified; not re-decided)

Found by searching `.scratch/blender-copilot/issues/` for the two titles the ticket
names:

- **`09-agent-loop-and-recovery.md`** — the per-tick state machine (`IDLE` →
  `REQUESTING` → `TOOL_QUEUE` → terminal; "the callback performs exactly one bounded
  step per tick: drain events, **or** execute one tool call, **or** finalize");
  `MAX_ROUNDS = 8`, `MAX_TOOL_CALLS = 24`, the repeat detector (third identical call,
  tool + canonical arguments), the 3-consecutive-all-failing-rounds stop; no retry
  anywhere, ever; `length` is terminal because a truncated call is *incomplete*, not
  wrong; **report, never unwind**; the shared flusher that answers every
  `tool_call.id` so Stop cannot leave an orphan.
- **`06-tool-contracts.md`** — fresh namespace per call with the Console prelude,
  `compile(..., "<model>", "exec")` then `exec` under stdout/stderr capture and an
  EOF stdin, `except BaseException`, the full traceback with the *model's* line,
  caps 4,000/2,000/2,000 with `… [N chars elided] …`, one shared 8,000-char result
  cap, `purpose` required at 1–80 characters, and the envelope
  `{ok, tool, summary, …, error:{kind, …}}`.

Both are transcribed rather than re-decided. Nothing in them turned out wrong.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/execution.py` | **new**, bpy-free: the schema, `elide`, `run_python` (fresh `__main__`, EOF stdin, BaseException, traceback + model line, caps), `parse_arguments`, `execute_tool` → `{ok, envelope, content, detail, summary}` |
| `blender_copilot/toolbox.py` | **new**, bpy side: the Console prelude (`bpy`, `C`, `D`, `math`, four `mathutils` types) and the executor the loop calls. Ticket 03 adds its two tools here |
| `blender_copilot/conversation.py` | the loop: `phase` (`idle`/`request`/`tool`), `pending` queue, the caps, `pump()`, the flusher, `attach()`; the **wire** history (`self.history`) kept apart from the display transcript; `finish_turn` → `finish_reply(reason, tool_calls)`; `cancel`/`fail_turn`/`clear` go through `_end_turn` |
| `blender_copilot/_worker.py` | accumulates fragmented `delta.tool_calls` by `index` (ids/names once, `arguments` concatenated) and delivers them **on `done`**; the request payload carries `tools` |
| `blender_copilot/transport.py` | `send(cfg, messages, tools=None)` — declared on every round |
| `blender_copilot/stream.py` | the loop's collaborators are wired once via `attach`; `_tick` does **exactly one** of drain / one tool call / finalize |
| `blender_copilot/panel.py` | Send declares the tool schemas |
| `blender_copilot/prompt.py` | the paragraph that said "this build has no tools" now names the one tool and the pre-mutation rule; `context()` returns `(base_prompt, live_summary)` |
| `tests/test_conversation.py` | 105 new checks: the sandbox, the envelope, and the loop driven through its seam with fakes |
| `tools/loop_wire_probe.py` | **new**: scripted localhost provider + real `_worker.py` + real loop + real sandbox — 26 checks, no billable request |
| `tools/loop_panel_probe.py` | **new**: real Blender GUI, real `bpy` sandbox, real timers, scripted transport — 28 checks and a screenshot |

### Evidence

**Gate 1 — the CPython suite** (167 checks; it was 62 after ticket 01):

```
$ python3 tests/test_conversation.py
…
all checks passed
$ python3 tests/test_conversation.py | grep -c '^ok '
167
```

New checks that carry this ticket, verbatim from that run:

```
-- run_blender_python --
ok   a name from one call is not visible in the next
ok   and the failure names the line it happened on
ok   the FULL traceback comes back
ok   stdout from before the raise is kept
ok   a syntax error is reported with its own line
ok   SystemExit cannot take the loop with it
ok   stdout is capped, and says it was
-- the tool's envelope --
ok   arguments arrive as a string and are parsed
ok   an unknown tool is named and runs nothing
ok   malformed arguments come back as raw text the model can fix
ok   a missing purpose is a self-explaining argument error
-- the loop --
ok   the assistant message with tool_calls is on the wire history
ok   the panel shows a code row naming what will run
ok   and a tool row naming it, still running
ok   a tick runs the queued call
ok   and only that one
ok   the result is on the wire against the call's id
ok   that tick did not also send a request
ok   the next tick asks for the following round
ok   the round carries the tool result the model asked for
ok   and the live summary is still the last message
-- a failing call --
ok   a failing call is an error row with the traceback behind it
ok   the model receives the traceback on the following round
ok   and the traceback is in that request, character for character
ok   the loop did not retry it
-- one tool call per tick --
ok   both rows exist before either runs
ok   the second is still the running row, so the panel can paint it
-- Stop mid-turn --
ok   every call the model made has a result
ok   the unrun calls are marked cancelled
ok   a stopped turn can be sent again
-- the caps --
ok   the round cap stops a converging loop
ok   after exactly the declared rounds
ok   and it names the cap rather than failing silently
ok   the same call is not run a third time
ok   the call cap bounds one round of parallel calls
ok   three all-failing rounds stop the turn
-- malformed arguments --
ok   the model is asked to continue, not to repeat
```

**Gate 2 — the panel draw check**, gated on the token and never on the status:

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
ok   variant=boxes    expanded=True  widgets=118 boxes=16 longest_label=61
ok   the whole transcript draws: error block and running tool included
ok   Stop replaces Send only while a turn is in flight

all draw bodies ran
SMOKE OK
Blender quit
BOUNDED | finished on its own in 0.8s | rc=0
```

No `SMOKE FAILED` in that output.

**The loop against a real worker, with the provider faked on localhost.** The
orchestrator's note reserves real requests to ticket 16, so the wire is driven by a
scripted `http.server` on `127.0.0.1`: the real `transport.Worker`, the real
`_worker.py` child, the real loop and the real sandbox, nothing off the machine and
nothing billed.

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 \
    tools/loop_wire_probe.py
WIRE | scripted provider on 127.0.0.1:51364 - no request leaves this machine
WIRE | ok   every round declares run_blender_python
WIRE | ok   the arguments arrived as a JSON string
WIRE | ok   the fragmented arguments survived whole
WIRE | ok   round two carried the assistant's tool_calls
WIRE | ok   the result answers the call's own id
WIRE | ok   the code's print came back to the model
WIRE | ok   the failing call's traceback is in the next request
WIRE | ok   and the change it made before raising is reported, not undone
SMOKE OK
BOUNDED | finished on its own in 0.9s | rc=0
```

(26 checks, all `ok`; log at `logs/loop-wire-probe.txt`. The call's `arguments`
arrive split across three SSE deltas and the check that they "survived whole"
compares the reassembled JSON to the code string that went out.)

**The tracer bullet, live.** `bpy.app.timers` never pump under `blender -b`, so this
is a windowed run: backgrounded, bounded, writing to a file, quitting Blender
itself.

```
$ python3 tools/bounded_run.py 120 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --python tools/loop_panel_probe.py
LOOP | tick 1: phase=tool drain_registered=True queued=0 rows=4
LOOP | turn 1 finished after 0.33s
LOOP | cube after turn 1: dimensions.z=3.000 scale.z=1.500
LOOP | ok   the cube changed, and no line of this probe ran the code
LOOP | ok   a code identity row was drawn
LOOP | ok   a tool row names what ran
LOOP | ok   its output is behind the expander, not in the row
LOOP | ok   the model received that traceback on the following round
LOOP | ok   the half-applied change is still there, not unwound
LOOP | ok   the timer repainted while the turn was in flight
LOOP | screenshot: {'FINISHED'} -> loop-panel.png, sidebar tab 'Copilot'
SMOKE OK
BOUNDED | finished on its own in 1.9s | rc=0
```

28 checks, no `FAILED`; log `logs/loop-panel.txt`. The cube is `size=2` before the
turn, so `dimensions.z` goes 2.000 → 3.000 through the sandbox alone — this probe
never touches the cube itself, and the code that moves it arrives as
`function.arguments` in a scripted tool call. Nothing ran in the foreground and no
request was made: the transport object is replaced, and the environment holds an
obviously fake key that the real transport never sees.

**Screenshot: `logs/loop-panel.png`** (cited, with what it does *not* show). It
shows the 3D Viewport with the cube visibly taller, and the Copilot sidebar reading
`prototype · idle`, the prompt "make the cube taller", the assistant's prose, a
collapsed code row `▸ Scale Cube 1.5x on Z · 6 lines` with
`→ full code in "Copilot Code"`, the tool row `▾ Scale Cube 1.5x on Z` with its
`output` expander closed, the closing "Done: the cube is 1.5x taller on Z.", and a
second turn whose failed call is an `alert`-red row with its `output` **open**. That
is checkbox 2 in pixels, in both expander states at once. It does **not** show the
output text inside the closed expander, and the sidebar is narrow enough that the
longest row is clipped at the region's right edge — a panel-width matter, not this
ticket's.

Two measurement notes that cost real time and are worth keeping:

- `Region.active_panel_category` is **read-only unless the sidebar has been laid
  out**: assigning it in the same timer tick that sets `show_region_ui = True`
  raises `AttributeError: attribute "active_panel_category" from "Region" is
  read-only` (the first screenshot therefore showed the *Item* tab and proved
  nothing about the panel). One tick later the same assignment works — that is why
  the probe opens the sidebar, returns, and selects the tab on the next tick.
- `bpy.ops.wm.splash()` is an **invalid operator call** from a script context, so
  the startup splash cannot be dismissed from Python; `show_splash = False` only
  affects windows created later. It does not cover the sidebar, so the panel is
  still photographable.

### Judgement calls the orchestrator should look at

1. **Where the caps are checked.** Ticket 09 says all four are "checked at the
   `TOOL_QUEUE → REQUESTING` boundary". Implemented literally, the call cap could
   not do the job its own parenthetical gives it — "bounds a round that returns many
   parallel calls" — because a round of thirty calls would run all thirty before the
   boundary was reached. So `_cap_reason()` is called at both boundaries a step can
   stop at: before the next queued call, and before the next request. The observable
   rules (8th round, 24th call, third identical, third all-failing round; stop and
   name the cap; never auto-continue) are exactly as ratified — this is about where
   the check sits, not what it does.
2. **The cap-flush result kind.** Ticket 09 names `cancelled` for the Stop path.
   Ticket 06's `kind` enum has no member for "never ran", so the cap path and the
   transport-death path flush with `kind: "not_run"` and a message naming the cap.
   A human may prefer to widen the enum instead.
3. **`prompt.py` needed a truth fix.** Its base prompt still said "This build has no
   tools: you cannot run Python" — true when written, and a lie the moment the tool
   is declared. A model told it has no tools will not call one, so the tracer bullet
   cannot fire live. I replaced that paragraph with the one tool and the
   pre-mutation rule, and left the rest of ticket 10's prompt (and the two read-only
   tools' descriptions) to build ticket 03, which owns that revision.
4. **`_tick` does one thing, not two.** My first version drained events *and* pumped
   in the same callback; ticket 09's "or" is load-bearing, because the tick that
   drains `tool_calls` is the tick that queues the `running…` row. Pumping in the
   same callback would run the call before the panel had drawn that row.

### Not done here, deliberately

The undo push and the receipt (build ticket 05), the two read-only tools (03),
persistence (04), the context projection (06) and Stop's execution budget (07) are
other tickets. `_end_turn()` is the seam 05 will push from, and
`session.attach(send, execute, context)` is where 03 adds its tools.

### What I could not verify

- **The live wire is untested.** No request reached DeepSeek: ticket 16 is the one
  that spends money and it is `human-required`, so `tools/transport_smoke.py` was
  not run. Everything about the real provider is therefore ticket 16's measurement,
  reused. In particular, the model's *own choice* to call the tool, and the
  provider's acceptance of the shape these requests now have (assistant message with
  `tool_calls` + matching `tool` results + trailing system summary), are not
  re-measured here — my checks assert the shape the provider was measured (ticket
  16) to require.
- **The sandbox was exercised with fake bindings** on CPython and real ones in the
  GUI probe; what was *not* tried is a long native call or a `while True:` loop under
  the real executor. That hazard is documented in the tool description and is build
  ticket 07's subject.
- **`get_scene_info` / `get_rna_info` do not exist yet**, so the resolution-order
  machinery is only exercised through `purpose` and the live summary.

## Orchestrator verification (2026-09-26)

Re-ran rather than read. Every command below was run by the orchestrator in a fresh
shell against this working tree:

- `python3 tests/test_conversation.py` → exit 0, `all checks passed`, 167 checks.
- the bounded draw smoke → `SMOKE OK` once, `SMOKE FAILED` **0** times,
  `BOUNDED | finished on its own in 0.8s | rc=0`.
- `tools/loop_wire_probe.py` → 26 checks, `SMOKE OK`, 0 `FAILED`, finished on its
  own in 0.8s. The scripted provider binds `127.0.0.1`, so nothing was billed.
- `tools/loop_panel_probe.py` → 28 checks, `SMOKE OK`, 0 `FAILED`,
  `cube after turn 1: dimensions.z=3.000 scale.z=1.500`, finished on its own in
  1.9s. Re-read `logs/loop-panel.png` and it shows what the answer claims: the cube
  taller, the panel `prototype · idle`, the collapsed code row, the tool row with
  its `output` expander closed, and the second turn's failure row with `output`
  open.
- the 14 load-bearing check lines re-grepped out of the orchestrator's own run, not
  copied from the transcript above.

**Verdict on the judgement calls.**

1. *Caps checked at both boundaries* — correct, and the literal reading would have
   been a bug: `_cap_reason()` (conversation.py:679) is called before the next queued
   call and before the next request (conversation.py:636), so a round of thirty
   calls cannot run all thirty. The ratified rules — 8 rounds, 24 calls, third
   identical, third all-failing round, stop and name the cap, never auto-continue —
   are unchanged. Accepted.
2. *`not_run`* — the right minimal choice, but say it plainly: this **amends ticket
   06's ratified `error.kind` enum**, which fixed four members (`exec_error`,
   `tool_argument_error`, `not_found`, `invalid_kind`) and no `not_run`. Ticket 09 §5
   designed `cancelled` for the user's Stop, and the user did not cancel here, so
   reusing it would have been a lie. The amendment needs the owner's ratification;
   nothing else in the build depends on the enum staying four. **Not settled by this
   ticket.**
3. *`prompt.py` truth fix* — necessary: the base prompt said "this build has no
   tools", and a model told it has no tools will not call one, so the tracer bullet
   could not have fired. It is disclosed, it is the minimum to make the ticket's own
   first checkbox true, and the rest of ticket 10's revision still belongs to build
   ticket 03. Flagged as the one place a later ticket may collide.
4. *`_tick` does one thing* — matches ticket 09's "drain events, **or** execute one
   tool call, **or** finalize". Correct.

**One side effect worth knowing, outside the repo.** The GUI probes run without
`--factory-startup`, so Blender rewrote
`~/Library/Application Support/Blender/5.2/config/userpref.blend` on quit. The probe
writes no preference — it sets environment variables only, and the addon's stored
values are loaded and written back unchanged — so no stored value was altered. But
the file's mtime moved, which matters because ticket 05.1 puts the API key in
exactly that file.

