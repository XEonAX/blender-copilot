# 07: Stop that actually stops

**What to build:** Stop that does something. Today the panel says it cannot
interrupt running code, which is honest but weaker than the truth: some things can
be stopped and some cannot, and the panel should say which is which — and then
actually stop the ones that can be.

**Blocked by:** 02 (One tool call, end to end)

**Status:** resolved
**Triage:** ready-for-agent

- [ ] Running a pure-Python infinite loop and pressing Stop ends it.
      **Split, and the split is the honest part.** A pure-Python infinite loop
      **does end** — measured, twice: 1.004 s against a 1.0 s budget and 2.00 s
      against a 2.0 s budget, with the turn closed, the call answered and one
      Ctrl+Z behind it. What does **not** end it is the *press*: Blender does not
      pump events while the main thread runs, so the click is not delivered, and
      the alarm ends the run instead. That is ticket 17's measured Stop table,
      which this ticket's Context tells me to follow ("the click is not
      delivered while the main thread runs; the alarm ends the run instead"), and
      the panel now says so in words. A user who presses Stop during a call still
      *sees* the loop stop; the causation is the budget. Left unticked rather than
      ticked against a claim the platform forbids — and Stop does end what the
      event loop can hand it: the stream, and the rest of a turn whose calls are
      still queued (ticket 02's `-- Stop mid-turn --`).
- [x] A long native call is **not** claimed to be stoppable, and the panel says so rather than pretending.
- [x] The panel's wording matches what was actually measured, in both directions — it must not under-promise either.
- [x] An interrupted turn still leaves a revertible undo step behind it.
- [x] The budget has a per-call and a per-turn figure, and both are named in the panel's copy.
- [x] Anything that can disarm the budget is either closed or stated plainly as disarmed.
      Closed as far as in-process closing goes (a disarm is detected, and its turn
      is ended, so switching the budget off buys one call and not the turn), and
      stated plainly where it cannot be closed at all — a `try/except
      BaseException` around the work is unstoppable, measured again here as a
      bounded child that had to be killed.

**Context:** *Runaway and hang protection* **measured** this: a line-level monitor
cannot stop a plain infinite loop at all, a wall-clock signal stops the loop, a
sleep and a blocking read cheaply, a long native call is only stopped once it
returns, and the budget can be disarmed in a line by the code it is meant to bound.
Follow the measurements, not the aspiration, and keep the honesty claim in step
with them.

## Answer

### Decisions this transcribes (ratified; not re-decided)

Found by searching `.scratch/blender-copilot/issues/` for the titles this ticket
names. Both are quoted rather than re-argued:

- **`17-runaway-and-hang-protection.md`** — §4's **unit and value**: "Wall-clock
  seconds, enforced by a repeating SIGALRM itimer. Value: **15 s per
  `run_blender_python` call, 60 s cumulative per user turn**", with a 1 s repeat
  and the rejection list (lines: misses `while True: pass` entirely; instructions:
  11.39× for nothing the alarm lacks). §3's three sentences, §3's Stop table, and
  the §2 measurements the design turns on (`recovery_push` returns
  `{'FINISHED'}` on the signal path; `handler_outliving` shows post-turn work
  outside everything).
- **`02-one-tool-call-end-to-end.md`** (build ticket, "One tool call, end to end",
  in `.scratch/blender-copilot-build/issues/` — the one this ticket is blocked by)
  — the loop's per-tick state machine, and through it `09-agent-loop-and-recovery.md`
  §0/§3/§5: one bounded step per tick, "report, never unwind", and the flusher that
  answers every `tool_call.id` so a stopped turn is still sendable. The piece this
  ticket leans on hardest is **`_end_turn`**, the single funnel every path out of a
  turn passes through: it is where ticket 12 §1's undo step is anchored, and it is
  therefore where the budget's turn clock is closed too — a turn that ended at its
  budget still ended, and the next turn must start with a full 60 s.

**One departure from §3's exact copy, stated rather than smuggled.** §3's
normal-stop sentence ends "This turn's changes are one Ctrl+Z step." The panel
already says that in the receipt box directly underneath — and the receipt is the
only part of the panel that *knows*, because with Global Undo off or in edit mode
Blender records no step and the receipt says "Not undoable". Two sentences, one
claiming revertibility and one denying it, on one screen, is the failure mode
being avoided. So the transcript sentence reports the budget and the receipt
carries the undo claim. The other two §3 sentences are transcribed as written
(native call / could not stop), and the "not stopping" warning is where it can
actually be read: in the permanent note **before** the call, because the panel
cannot repaint while the code runs. Everything else in §3 is on screen verbatim.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/budget.py` | **new, bpy-free**: `CALL_SECONDS = 15` / `TURN_SECONDS = 60` / `REPEAT_SECONDS = 1` / `LATE_SECONDS = 0.25`, `Exceeded(BaseException)`, `Limits` (the two clocks, the itimer, the read-back, the verdict), `LIMITS` (the process's one instance), `RUNNING_LINES` (the panel's copy), `verdict_note` (model-facing), `stopped_sentence` (user-facing), `stopped(verdict)` |
| `blender_copilot/execution.py` | `run_python(code, purpose, bindings, limits=None)` opens the budget window around **`exec` only**, catches `Exceeded`, and returns `error.kind == "budget"` with `seconds`/`limit`/`late` plus `budget: verdict`; a swallowed or disarmed call returns `ok: true` *with* the verdict, so it is not reported as clean. `_sibling` now loads any sibling by name (and reuses an already-loaded `bc_<name>`); `execute_tool(..., limits=None)` |
| `blender_copilot/conversation.py` | `session.limits`; `begin_turn` opens the turn clock, `_end_turn`'s `finally` closes it; `_budget_verdict(result)` reads the verdict out of the **envelope** (so the loop never learns what a signal is); `_run_call` returns a stop reason and `pump()` routes it through `_stop_with` — the same "report, never unwind" path the caps use |
| `blender_copilot/panel.py` | the running note is drawn from `budget.RUNNING_LINES` (one copy, not two); Stop's `bl_description` corrected; **error rows now wrap 5 characters earlier** (`ERROR_ICON_INSET`) — see *Found by looking* below |
| `blender_copilot/prompt.py` | the CAPABILITY paragraph no longer claims code "cannot be cancelled or time-limited"; it states the measured contract (15 s / 60 s, what stops, what only stops on return, what cannot be stopped) |
| `tests/test_conversation.py` | **+59 checks** (359 total) |
| `tools/panel_draw_smoke.py` | **+5 checks** |
| `tools/budget_probe.py` | **new**: the headless measurement, 52 checks, one `BC|{json}` line per case, plus `BC_BUDGET_CASE=swallow_hang` for the case whose subject is a hang |
| `tools/budget_panel_probe.py` | **new**: the GUI run — 22 checks, and two screenshots |

The push discipline needed nothing: `conversation._end_turn` is already the funnel
ticket 12 §1's undo step hangs off, and ticket 17 §2 measured that the raise
arrives *through* the model's frames, so that funnel is reached. Case 1 measures
the consequence rather than trusting it.

### Evidence

**Gate 1 — the CPython suite** (359 checks; 300 before this ticket):

```
$ python3 tests/test_conversation.py > /tmp/... 2>&1; echo "RC=$?"
RC=0
$ grep -c '^ok ' ...
359
all checks passed
```

The 59 checks that carry this ticket, verbatim from that run:

```
-- the budget --
ok   the panel's running copy names the per-call figure
ok   and the per-turn figure
ok   the shipped figures are ticket 17's, not a tune-up
ok   the copy promises a pure-Python loop stops
ok   it does not claim a native call stops once it has started
ok   it says Stop cannot be delivered while the code runs
ok   and it says what cannot be stopped at all
ok   a plain infinite loop is interrupted at all
ok   at its own budget rather than after it
ok   and the interrupt says which budget it was
ok   the timer is off again once the call is over
ok   and the handler the session had before is back
ok   a sleep is interrupted too, not waited out
ok   code that catches the interrupt is interrupted again
ok   so catching it buys one tick, not the rest of the call
ok   code that switches the timer off is noticed
ok   and the verdict does not pretend it was interrupted
ok   so is code that replaces the handler
ok   and the handler is restored afterwards
ok   the turn's cumulative budget ends a call early
ok   and it says so, rather than blaming the call
ok   against the turn's remaining seconds
ok   the turn's clock stops with the turn
ok   the next turn starts it again
ok   a call that starts after the turn's budget is gone is refused
ok   and the envelope names the budget
ok   the code never ran
ok   and the model is told nothing ran, rather than that something was interrupted
ok   and the loop reads that refusal as a stop
ok   so a refused call ends the turn too
ok   and the panel says which budget was gone
ok   the sandbox reports the interrupt instead of raising it
ok   the envelope calls it a budget stop, not a traceback
ok   and says how long the code actually ran
ok   what it printed before the interrupt is kept
ok   the model is told the call did not finish
ok   the wire content is a valid envelope
ok   code that swallows the interrupt still returns a result
ok   but the result says an interrupt was delivered
ok   and it is not reported as a clean call
ok   the code's own output is still there
ok   the interrupted call's row is an error, not a running row
ok   the turn ended rather than asking for another round
ok   and no second request went out
ok   the interrupted call is answered, so history stays sendable
ok   the panel says the code ran past its budget
ok   and it does not claim the turn was cleaned up
ok   an interrupted turn still closes its undo record, so Ctrl+Z has a step
ok   a turn opens the budget's turn clock
ok   and ending the turn closes it
ok   a swallowed interrupt still ends the turn
ok   and the panel names what happened
ok   with the undo step still written
ok   code that switched the budget off ends the turn too
ok   and the panel says the budget was switched off, not that it fired
ok   a call inside its budget is not stopped
ok   and its row is ok
ok   so the loop does ask for the next round
ok   and the turn is streaming again
```

**Gate 2 — the draw smoke**, gated on the token and never on the status:

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
ok   the running-call note draws the budget's own copy: what stops, what does not, and what Stop cannot do
ok   and it goes again when nothing is running
ok   a stopped call's sentence draws, with the seconds it actually ran
ok   the tool description and the prompt name the same two figures as the budget, and neither still promises no time limit
ok   both halves share one budget object, so there is one 60s ledger
all draw bodies ran
SMOKE OK
BOUNDED | finished on its own in 0.8s | rc=0
```

`SMOKE FAILED` appears **0** times in that output. The last of those five is the
coupling check: `conversation.session.limits is budget.LIMITS is
execution.budget.LIMITS`. If the package ever resolved two copies of `budget.py`,
the turn clock and the call window would be two different 60 s figures and the
failure would be silent — which is exactly how the CPython suite's first run
failed while I was building this (two copies of `Exceeded`, so `run_python`'s
`except` missed its own exception; `_sibling` now reuses `bc_<name>`).

**The measurement itself — `tools/budget_probe.py`**, headless, real sandbox, real
loop driven through `stream._tick()`, real `undo_push`:

```
$ python3 tools/bounded_run.py 120 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/budget_probe.py
BUDGET | blender 5.2.2 LTS, background=True
BUDGET | scene prepared: ['Cube'], active=Cube
...
BUDGET OK          (52 checks, no FAILED)
BOUNDED | finished on its own in 8.5s | rc=0
```

One `BC|{json}` line per case (log: `logs/budget-probe.txt`, and the per-case
verdicts below are lifted from it). The shipped numbers are shortened so the whole
probe fits in seconds; the last column is the sentence the panel showed.

```
case            elapsed  verdict                                             sentence shown
loop             1.004   interrupted, interrupts 1, kind call, late false    ⏱ Stopped after 1.0s — the code ran past its budget.
                         limit 1.0, seconds 1.001, row error, requests 1
                         stdout "Cube: z scale is now 2.00\n"
                         receipt {'undoable': True, 'label': 'copilot: Spin forever', ...}
                         one undo of the stopped turn -> {'FINISHED'}  (scale.z 2.0 -> 1.0)
sleep            1.002   interrupted, as above (limit 1.0)                  ⏱ Stopped after 1.0s — the code ran past its budget.
recv             1.004   interrupted, as above (limit 1.0)                  ⏱ Stopped after 1.0s — the code ran past its budget.
native           2.573   interrupted, kind call, late TRUE                  ⏱ Stopped after 2.6s — a single Blender/NumPy call
                         limit 0.3, seconds 2.569                            cannot be interrupted once it has started.
swallow          0.306   interrupted FALSE, interrupts 1, row ok            ⚠ The code caught the interrupt and kept running —
                         limit 0.3, turn stopped                            the call ran 0.3s past its budget. The turn stops here.
swallow_twice    0.805   interrupted, interrupts 3, late false               ⏱ Stopped after 0.8s — the code ran past its budget.
disarm           0.001   interrupted false, disarmed TRUE, row ok            ⚠ The code switched the budget off — nothing
                         interrupts 0, turn stopped                         bounded that call. The turn stops here.
turn             0.502   interrupted, kind TURN, limit 0.5                   ⏱ Stopped after 0.5s — this turn has used its 60s
                                                                            of call time.
clean            0.000   interrupted false, interrupts 0, disarmed false      (none) — the turn finished normally, 2 requests
                         row ok
```

So box 1's stop is a date and not a claim: **a plain `while True: pass` is
interrupted at its budget** — and box 4 is measured, not inherited: the code
scales the cube to 2.0 *before* the loop, the loop is stopped, `scale.z` is still
2.0, the receipt says `Undoable`, and one `bpy.ops.ed.undo()` (the operator Ctrl+Z
invokes) puts it back to 1.0.

Case 4 is the one the copy had to change for, and the size is a measurement rather
than a guess: at 1400² the SVD returned in 0.44 s, which is *not* late, so that
first version of the case measured nothing. Bundled NumPy 2.3.4 here: 0.39 s at
1400², 1.12 s at 2000², 2.38 s at 2400², 4.69 s at 2800². 2400² overruns a 0.3 s
budget by 8× `LATE_SECONDS`.

`late` is an inference, and it is deliberately narrow: **one** raise, arriving
more than a quarter-second after its own deadline, is a raise that waited for a
native call to return. The swallow-twice case is the control that forced the
`interrupts == 1` clause — its *third* raise takes at 0.81 s against a 0.30 s
budget, and the first version of the rule called that a late native call and put
that sentence on screen. The probe now checks the sentence is not that one.

**The unstoppable case, in its own bounded child** (`BC_BUDGET_CASE=swallow_hang`),
which is the one case that must never be run unguarded:

```
$ BC_BUDGET_CASE=swallow_hang python3 tools/bounded_run.py 15 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/budget_probe.py
BUDGET | BC|{"case": "swallow_hang", "expect": "never returns"}
BUDGET | this child will be killed by the driver: that kill IS the observation
BOUNDED | KILLED at the 15s deadline after 15.0s | the case never returned -- that IS the result for a hang case
```

Code that catches the interrupt forever is not stopped by anything in the process:
ticket 17's HUNG row reproduces against this implementation, which is why the
panel's copy calls it unstoppable instead of implying the budget always wins. The
probe writes `logs/budget-probe-hang.txt` *before* the call for exactly this
reason — the run that gets killed has no verdict to write.

**The panel, in a window** — `tools/budget_panel_probe.py`, 22 checks, driving
`stream._tick()` itself with the add-on's timer stopped so it can photograph the
one tick where the note is up and the call has not run yet:

```
$ python3 tools/bounded_run.py 120 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --python tools/budget_panel_probe.py
PANEL | ok   the rows are queued but nothing has run
PANEL | ok   the status says code is running
PANEL | ok   the queue holds the call, unrun
PANEL | ok   and the cube is untouched so far
PANEL | the call ran for 2.00s against a 2.0s budget
PANEL | verdict: {'armed': True, 'kind': 'call', 'seconds': 2.001, 'limit': 2.0, 'interrupts': 1, 'interrupted': True, 'late': False, 'disarmed': False}
PANEL | ok   the panel names what happened, with the seconds it ran
PANEL | ok   the change it made before the loop is still applied
PANEL | ok   and the receipt says the step is undoable
PANEL | one undo of the interrupted turn -> {'FINISHED'}
PANEL | ok   one Ctrl+Z reverts the interrupted turn
PANEL | ok   no alarm is left armed
BUDGET PANEL OK        (22 checks, no FAILED)
BOUNDED | finished on its own in 4.0s | rc=0
```

**Screenshots** (`logs/budget-panel.png`, `logs/budget-stopped.png`), and what each
one does and does not show.

`budget-panel.png` is the running state: header `prototype · running code`, Stop in
Send's slot, and the note box reading, in full, legibly, nothing clipped —
`Budget: 15s per call, 60s per turn.` / `A Python loop, a sleep or a blocked read
stops at the budget.` / `A single Blender or NumPy call only stops once it
returns.` / `Stop cannot be delivered while the call is running.` / `A call that
swallows the interrupt, or switches the budget off, cannot be stopped: force-quit
Blender, and this turn's changes may not be undoable.` That is boxes 2, 3, 5 and 6
in pixels at once, and the cube in the viewport is still at scale 1.0 — the note is
photographed before the call runs, which is the only moment it exists.

`budget-stopped.png` is afterwards: `idle`, Send back, the code identity row and
the tool row with its `output` expander, the error row **`⏱ Stopped after 2.0s —
the code ran past its budget.`**, and the receipt `Undoable / the scene's data
changed / Ctrl+Z reverts this turn.` The cube *is* visibly stretched on Z: the
half-applied change stands, which is box 4's other half and the reason the receipt
matters. What the pictures do **not** show: any timing (a still cannot show that
the run took 2.00 s — the log does), and the numbers in the note are the shipped
15 s/60 s by design while that run was shortened to 2 s/30 s by the probe, which
the log says out loud.

**Found by looking, not by counting.** The first `budget-stopped.png` showed the
stop sentence mangled: `⏱ Stopped after 2.0s — the co…` with `past its budget.` on
the next line. A 33-character first line was clipped while a 36-character line
*without* an icon (the note's own, in the same run) drew whole — the `ERROR` icon
eats the difference. `panel.py` now wraps error rows to `budget - ERROR_ICON_INSET`
(5), documented with that measurement; the second screenshot shows the sentence
whole. The stub layout counts widgets, not pixels, so no headless check could have
caught it, and the label-length heuristic in the smoke treats 33 characters as
fine.

**No regressions** (all run this session, none required by the ticket; the Blender
ones are bounded and each was grepped for its token, not its status):

```
$ python3 tests/test_store.py               → all checks passed
$ python3 tests/test_context.py             → all checks passed (60)
$ python3 tests/test_read_only_tools.py     → all read-only tool checks passed
$ python3 tools/bounded_run.py 180 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/undo_step_probe.py
  → grep -cE 'UNDO \| ok ' = 53 ; SMOKE OK ; BOUNDED | finished on its own in 1.3s | rc=0
$ python3 tools/bounded_run.py 120 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --python tools/loop_panel_probe.py
  → grep -cE 'LOOP \| ok ' = 28 ; SMOKE OK ;
    LOOP | cube after turn 1: dimensions.z=3.000 scale.z=1.500 ;
    BOUNDED | finished on its own in 2.3s | rc=0
$ python3 tools/bounded_run.py 90 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/read_only_tools_probe.py
  → 108 ok ; SMOKE OK ; BOUNDED | finished on its own in 0.8s | rc=0
```

No network path was touched, so `tools/transport_smoke.py` was **not** run: it
makes real billable requests and nothing in this ticket needs one.

**One pre-existing failure, not mine, named so it cannot be mistaken for one.**
`tools/loop_wire_probe.py` fails 7 checks — and it fails identically at `HEAD`,
which is how I know: `execution.prelude(world)` accepts either an object with
`bindings()` or a *callable* returning the prelude dict, and the wire probe passes
a plain dict, which is neither, so every call in it runs with an empty prelude and
dies with `NameError`:

```
$ git show HEAD:blender_copilot/execution.py > /tmp/headcheck/execution.py   # + guides.py, budget.py beside it
$ python3 -c "...load /tmp/headcheck/execution.py..."
HEAD prelude(dict) -> {}
HEAD call ok -> False | NameError
```

The one-line fix belongs to its owner (ticket 02's artifact):
`execute=lambda call: execution.execute_tool(call, lambda: PRELUDE)`. I did not
make it — this ticket does not own that probe, and a silent edit there would be an
unexplained change in the diff. It is not evidence about this ticket either way:
this ticket's evidence for the loop is Gate 1, `tools/budget_probe.py` (the real
sandbox and the real loop under Blender) and the two screenshots.

### Three things a reviewer should decide against

1. **A hole this ticket's own tests closed, which is worth reading as a hole and
   not as a detail.** `budget.stopped(verdict)` first required
   `verdict["armed"]` - and a call *refused at the door* (the turn's 60 s already
   spent, so there is no window to arm) is never armed. The effect was that the
   one verdict saying "this turn has nothing left" was the one verdict the loop
   ignored: an error row, then another round, up to the caps. It is fixed
   (three flags, no `armed`) and the case is checked at both levels - the refused
   envelope, and the turn ending on it - which is how it was found.

2. **`error.kind == "budget"` widens ticket 06's envelope enum.** That enum had no
   member for "the code was stopped", so the interrupted call reports
   `kind: "budget"` with `budget_kind`/`seconds`/`limit`/`late`, and the other two
   verdicts (swallowed, disarmed) ride in a separate `envelope["budget"]` because
   those calls *succeeded*. A human may prefer to widen ticket 06's enum instead;
   the loop reads one field either way (`_budget_verdict`).
3. **An interrupted call ends the turn.** Ticket 17 §4 makes the budget per call
   *and* per turn, and the per-turn figure only means something if a turn cannot
   shrug off a blown call. So a budget stop, a swallowed interrupt and a disarmed
   budget each end the turn on the cap path — the model gets the envelope on the
   *next* turn, which is the same "report, never unwind" policy ticket 09 §3 sets
   for the caps. The alternative (let the model see it and continue) would hand a
   runaway another 60 s.

### What I could not verify

- **That a Stop click is delivered during a run**, because it is not: this is the
  one box left unticked, and the reason is structural rather than a missing probe.
  I did not fake a click from a thread to "show" it — that would have measured my
  own flag rather than the user's.
- **Whether the code was *mid-mutation* when the alarm fired.** Every case here
  mutates and then loops, so the mutation is complete before the interrupt; what
  happens when the raise lands between `obj.scale.z = 2.0` and
  `view_layer.update()` is not measured, and the receipt's before/after diff is
  exactly the thing that would not see it (the `depsgraph_update_post` flag is what
  covers that class, ticket 05).
- **Post-turn work** (`bpy.app.handlers`, `bpy.app.timers` registered by a turn):
  ticket 17 §3 has a sentence for it and says the timer half is GUI-only. It is not
  implemented here and not claimed: this ticket's budget bounds *calls*, and a
  turn's registration outlives it exactly as ticket 17 measured.
- **Whether the alarm interacts with Blender's own use of `SIGALRM`.** No
  interference was observed in any run above (including the sweeps), and the
  previous handler is restored and the timer cleared on every exit path — asserted
  in both probes — but "Blender never uses SIGALRM" is not something I measured.
- The tool row's purpose label carries the same `ERROR`/`CHECKMARK` icon this
  ticket's error-row fix is about, so a *long* purpose can clip the same way. Not
  measured, not touched: it is ticket 02/03's row.

