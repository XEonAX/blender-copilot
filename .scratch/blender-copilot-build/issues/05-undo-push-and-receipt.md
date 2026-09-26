# 05: The undo push and the receipt

**What to build:** a change the agent made can be taken back with one Ctrl+Z, and
the panel tells the user so. **This is the ticket that can corrupt a real file** —
everything else can be re-run, this cannot — so it is the one that keeps the heavy
verification.

**Blocked by:** 02 (One tool call, end to end), 04 (Persistence)

**Status:** resolved
**Triage:** ready-for-agent

- [x] After a turn that changed the scene, one Ctrl+Z reverts exactly that turn — not the previous one, and not two turns.
- [x] The step is labelled with the turn, and the label appears verbatim in Blender's undo history.
      **Split, and the split is the honest part.** A command in this session shows
      the label is built from the turn and reaches `ed.undo_push` as its `message`
      (`undo_step_probe.py` case 1 records `['copilot: before the first change',
      'copilot: make the cube taller']` from a wrapper around the shipped `_push`).
      The *verbatim display* is not re-measured here — the undo stack has no RNA
      to read back, and my attempt to photograph the history popup failed (see
      *What could not be verified*). That half rests on ticket 15 case B, where
      the owner read the string in Undo History.
- [x] A turn that changed nothing does not push.
- [x] The push happens at the **end** of the turn, in a `finally`, so an interrupted turn still leaves a revertible step.
- [x] Pushing is **refused** while an object is in edit mode, not attempted.
- [x] The panel shows a receipt naming what changed, plus the shortcut for Blender's undo history.
- [x] With Global Undo off, auto-run pauses and the panel says so.
- [x] The receipt is honest that undoing covers local scene data only — not files, not network, not preferences.

**Context:** *What replaces undo as the recovery mechanism?* fixes the push
discipline, and *Confirm the four undo cases in a GUI* **measured** it — pushing at
the end works, one push covers a multi-operation turn, pushing before the change
reverts too far, Global Undo off makes undo raise rather than no-op, and a push
taken in edit mode records nothing usable and the undo after it **deletes the
object**. Those are measurements, so treat them as constraints rather than
hypotheses.

## Answer

### Decisions this transcribes (ratified; not re-decided)

Found by searching `.scratch/blender-copilot/issues/` for the two titles the
ticket names:

- **`12-recovery-mechanism.md`** — §1's push discipline (the unit is the user's
  turn, one push at the end, in a `finally`, only if the turn mutated, never
  `undo=True`, plus the baseline guard); §3's receipt as a pre/post diff of the
  bounded `get_scene_info` summary with its datablock-level honesty limit; §4's
  Global-Undo-off **pause** and edit-mode refusal; §5's sentences; and the
  ratification block, which rejected §6 (auto-run stays unscoped) and changed
  nothing else.
- **`15-undo-gui-confirmation.md`** — the five measured cases. Three are
  constraints on this code: push at the end reverts exactly the turn, push at the
  start reverts too far, and **a push in edit mode records nothing usable while
  the undo after it deletes the object**.

Two of those claims turned out to be imprecise or wrong. They are recorded below
rather than quietly worked around, and neither changes the design.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/undo.py` | **new**, 381 lines, **no `bpy`**: the label (`copilot: ` + 40 chars, cut at a word), the mutation diff over the bounded summary, the receipt (`title`, `changed`, `more`, `lines`), the detectable outside-the-blend effect, and `pause_reason` — the whole rule, on plain CPython |
| `blender_copilot/undo_blender.py` | **new**, the `bpy` half: the before-image, the `depsgraph_update_post` flag, `_push` (one `ed.undo_push`, treating anything but `FINISHED` as a failure), `open_turn`/`close_turn`/`forget`, and `pause_reason(context)` |
| `blender_copilot/conversation.py` | the `undo` collaborator on the existing `attach` seam, called from `begin_turn` and from a **`finally` in `_end_turn`**; `next_call`; `call_name`; `last_receipt` cleared when a turn opens; `COVERAGE_LINES` now §5's two sentences |
| `blender_copilot/stream.py` | the baseline guard immediately before a `run_blender_python` runs; the turn-end persist stays in the tick's `finally` |
| `blender_copilot/panel.py` | Send refuses through `undo_blender.pause_reason(context)`; the pause banner is built from `undo.PAUSE_LINES` and offers the fix; `_draw_receipt` draws `title`/`changed`/`lines`, alert-styled when not undoable; Send is disabled from the same one answer |
| `blender_copilot/scope.py` | `undo_blender.forget()` before `abandon()` on a file switch |
| `blender_copilot/__init__.py` | registers and unregisters the handler |
| `tests/test_conversation.py` | **+56 checks**, 259 total: the turn's undo record (open/close on every path out, including one where `_end_turn` itself raises) and the whole rule |
| `tools/panel_draw_smoke.py` | +7 checks and a receipt built by the real rule against a real change to the run's scene |
| `tools/undo_step_probe.py` | **new**, 813 lines: nine cases against the real undo stack, 53 checks headless / 54 with a window, plus a screenshot |

The push is anchored where the decision says it should be: `_end_turn` is the one
funnel every path out of a turn passes through — a normal reply, Stop, a cap, a
failed transport, a raise in the model's own code — so a tick-based observer could
*not* have made the claim, because Stop ends the turn inside the operator that
handled the click and the next tick never sees the transition. `close_turn()` sits
in a `finally` inside that funnel, so even a path that dies halfway through ending
a turn still leaves a step.

### Evidence

**Gate 1 — the CPython suite** (259 checks; 203 at ticket 04's close):

```
$ python3 tests/test_conversation.py; echo "EXIT=$?"
…
all checks passed
EXIT=0
$ grep -c '^ok ' (captured output)
259
$ awk '/-- the turn.s undo record --/,0' … | grep -c '^ok '
56
```

**Gate 2 — the draw smoke**, gated on its token, never on Blender's exit status:

```
$ python3 tools/bounded_run.py 60 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/panel_draw_smoke.py; echo "EXIT=$?"
ok   the receipt names what changed, the shortcut and the coverage line
ok   a turn that changed nothing draws no receipt
ok   Global Undo off draws the pause banner and the one-click fix
ok   and Send is drawn disabled while auto-run is paused
ok   edit mode pauses it too, with its own banner and no button
ok   with neither pause there is no banner and Send is live
SMOKE OK
EXIT=0
```

**The undo itself — `tools/undo_step_probe.py`**, nine cases against the real
stack. Every check is `bpy.ops.ed.undo()`, which is what Ctrl+Z calls; the only
fake is the transport:

```
$ python3 tools/bounded_run.py 150 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/undo_step_probe.py
--- case 1: a mutating turn, one undo
cube scale.z before: 1.0
cube scale.z after: 1.5
ok   the code ran and the cube really changed
ok   and the receipt says the turn is undoable
ok   the receipt is honest that the bounded summary cannot name this change
ok   the step is labelled with the turn
ok   the label is what the push was called with
pushes so far: ['copilot: before the first change', 'copilot: make the cube taller']
one undo -> {'FINISHED'}
ok   one undo reverts the turn
ok   and touched nothing else
--- case 2: a named change
receipt.changed = ['objects: 1 → 2', 'EMPTY: none → 1']
--- case 3: a turn that changed nothing
ok   no undo step was pushed
ok   and the panel shows no receipt
one undo after the read-only turn -> {'FINISHED'}
ok   one undo skips the turn that changed nothing
--- case 4: Stop mid-turn
ok   the second call is still queued when Stop is pressed
stop -> {'FINISHED'}
ok   an interrupted turn still gets a receipt
ok   labelled with the turn that was interrupted
one undo of the interrupted turn -> {'FINISHED'}
ok   one undo reverts the partial turn
--- case 5: a turn that ends in edit mode
mode after the turn: EDIT_MESH
ok   no push was attempted in edit mode
ok   and it is not undoable
ok   the reason is edit mode
ok   it does not claim Ctrl+Z reverts the turn
--- case 6: Global Undo off pauses auto-run
send with Global Undo off -> RuntimeError: Error: Global Undo is off. Auto-run is paused
ok   Send is refused
ok   nothing was sent to the transport
… 53 checks, SMOKE OK, BOUNDED | finished on its own in 0.9s | rc=0
```

Box 3's second check is the one worth reading twice: after a read-only turn, **one**
Ctrl+Z skipped that turn and reverted the previous mutating one. Had the read-only
turn pushed, that undo would have landed on its own snapshot — nothing visibly
reverted — and it would have taken two. That is a black-box measurement of "no
push", not a reading of a flag the addon set itself.

**With a window**, same file, same cases (the rest of the log is `logs/undo-step.txt`):

```
$ python3 tools/bounded_run.py 180 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --factory-startup --python tools/undo_step_probe.py
--- case 7: CONTROL - what a push does with Global Undo off
undo_push with Global Undo off -> {'FINISHED'}
undo with Global Undo off     -> {'FINISHED'}
--- case 8: CONTROL - a push taken in edit mode, then one undo
undo_push in edit mode -> {'FINISHED'}
objects 4 -> 3; removed=['Probe7']; mode=OBJECT
ok   and the undo after it removes the object being edited
--- photograph: one turn on a cleared conversation
photographed receipt: Undoable / ['objects: 4 → 5', 'EMPTY: 1 → 2']
UNDO | screenshot: {'FINISHED'} -> undo-step.png
… 54 checks, SMOKE OK, BOUNDED | finished on its own in 2.1s | rc=0
```

`logs/undo-step.png` is the panel with the receipt in it, and it is the evidence
for boxes 6 and 8: `Undoable`, `objects: 4 → 5`, `EMPTY: 1 → 2`,
`Ctrl+Z reverts this turn.`, `Undo covers local scene data only: not files,
network, preferences or Python state.`, `Ctrl+Alt+Z opens Blender's undo
history.` — all legible, none clipped. (The shortcut is Blender's own:
`presets/keyconfig/keymap_data/blender_default.py:865` in the **installed** 5.2.2
maps `ed.undo_history` to `Ctrl+Alt+Z`.) The transcript above it was cleared first,
because the receipt is drawn *below* the transcript by design and in a nine-case
conversation it is off the fold — the turn photographed is a real one, through the
real push.

**No regressions** (all run this session, none required by the ticket):

```
$ python3 tests/test_store.py              → all checks passed
$ python3 tests/test_read_only_tools.py    → all read-only tool checks passed
$ python3 tools/bounded_run.py 150 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --python tools/loop_panel_probe.py   → 28 LOOP | ok, SMOKE OK
```

The smoke earned its keep while this was being written: it caught a live
`NameError: name 'budget' is not defined` in `_draw_header` — the banner would
have crashed the panel's draw on every repaint with Global Undo off or in edit
mode. That is now fixed and the check that found it is in the file.

### Where a ratified claim turned out wrong or imprecise

**1. Ticket 12's "second, mechanical detector" for Global Undo off does not
exist.** Its ratification block says: "attempting the push fails, so the addon
need not read the preference to know undo is dead." Measured, in both modes:
`bpy.ops.ed.undo_push()` returns `{'FINISHED'}` with the preference off. The push
is not a detector, which is why `undo.pause_reason` reads the preference, and why
`_push` treats only a non-`FINISHED` return as failure. The rest of §4 stands.

**2. Ticket 15's case-4 conclusion is true only for a session with nothing behind
the push.** It says that with Global Undo off `ed.undo()` raises and "nothing is
reverted". In a fresh session that reproduces exactly (this probe, headless:
`RAISED RuntimeError: Operator bpy.ops.ed.undo.poll() failed`). In a *used*
session it does not: with an existing stack, `ed.undo()` returns `{'FINISHED'}`
and jumps to a state older than the push (the windowed run, above). That also
explains run 1's reading, which ticket 15 dismissed as "contaminated" — it was a
real second behaviour rather than a bad measurement, and ticket 15's own control
(same context, different preference, different outcome) is what makes both
readable. It **strengthens** §4: with the preference off, the Ctrl+Z a user might
reach for either refuses or rolls back past work they did not ask to change.
Either way the agent's turn is not recoverable, which is the whole case for
pausing.

**3. `depsgraph_update_post` does not fire on the write; it fires on the
evaluation.** Ticket 12 §1 offers the flag as "the only usable mutation signal"
and describes it firing "on a bare RNA write". Measured: `obj.scale.z = 1.5`
produces **0** events, and the `view_layer.update()` after it produces **1** (a
read-only update produces 0, a selection change 0, an operator called from Python
2). That ordering is load-bearing rather than trivia: `close_turn` must read the
after-image *first*, because reading the summary is what evaluates the depsgraph.
Checked the other way round, the flag would be read before the evaluation it is
waiting for, and the commonest turn there is — a scale on an existing object,
which the bounded summary cannot see — would go unpushed.

### Choices made inside this ticket (stated, not buried)

- **`is_dirty` and `is_saved` are not part of the receipt's diff.** `is_dirty`
  flips on every mutation, so it would appear on every receipt and say nothing: a
  scale on the existing cube would come back as `unsaved changes: no → yes`, which
  reads like a save and is not one. `is_saved`'s interesting transition *is* a
  save, and `outside_effects` owns that sentence. `filepath` stays in the diff
  because a Save As is worth naming in both places.
- **A turn that changed nothing clears the receipt** rather than leaving the
  previous turn's up: the receipt says "Ctrl+Z reverts this turn" and is drawn
  under the transcript, so a stale one reads as a claim about the wrong turn.
- **The baseline marker is marked attempted even when its push fails**, so a
  session where every code call retried a failing push is impossible; the
  turn-end receipt already reports a refused push.
- **`forget()` on a file switch, and only there.** A load discards the undo stack,
  so a step judged after one would record the *new* file's state under the old
  turn's label. `Clear` deliberately does not forget: the aborted turn's change is
  real and stays revertible.
- **The `finally` is inside `_end_turn`, not in `_tick`.** Both would satisfy "at
  the end of the turn"; the funnel is the one that covers Stop and Clear, which
  end a turn outside a tick.

### What I could not verify

- **The label rendering verbatim in Undo History.** `wm.bl_rna` exposes no
  undo-stack property (ticket 12), so there is nothing to read back. I tried to
  photograph it: `bpy.ops.ed.undo_history("INVOKE_DEFAULT")` reports `FINISHED`,
  but the `bpy.ops.screen.screenshot` taken 0.4 s later shows the window without
  the popup. Either a popup opened from a timer and never touched closes itself
  before the capture, or that operator does not capture popups — the attempt does
  not distinguish the two, so it establishes nothing either way. Not attempted
  again: the mechanism is ticket 15 case B's, and it is the owner's observation,
  not a script's.
- **The receipt in the *fold*, on a long conversation.** The screenshot needed a
  cleared transcript. Ticket 12 §5 also asks for the coverage sentence
  "under the input, always", and `panel.py`'s draw order puts the coverage box
  *after* the transcript and the receipt (`panel.py:456-458`), so with a long
  transcript it is off the fold — which is what my first attempt at this picture
  showed: the panel, with no receipt and no coverage block in it. That is a
  placement question the owner's 2026-09-26 visual pass settled for the receipt
  and did not speak to for the coverage box, so I left the order alone rather than
  move something a human already looked at.
- **§4's two escape hatches are not built.** The banner offers "Turn Global Undo
  on" (it already existed); it does **not** offer the per-turn "Run once anyway",
  and the edit-mode banner offers no "exit to Object mode" button. Both are in
  §4's ratified prose. Neither is in this ticket's eight boxes, and both are
  *bypasses* of the only recovery mechanism in the ticket that "can corrupt a real
  file" — so I left them out rather than ship an unrequested bypass, and this line
  is what a human should ratify or reject.
- **What a raise *inside* `_tick` does.** A raise that escapes `pump()` leaves the
  turn open, so no step is written and the session stops advancing. Every
  realistic interrupt is caught (`run_python` catches `BaseException`, and Stop,
  caps and transport failures all funnel through `_end_turn`), so this is a
  pre-existing hole rather than one this ticket opened — but it is real, it is the
  one path that reaches neither the step nor the receipt, and it is ticket 07's
  subject.
- **Reconciliation after an external Ctrl+Z.** Ticket 12 §3 lists a third job for
  the diff — feeding `undo_post`/`redo_post` so the receipt stops advertising a
  step the user already walked back. No box asks for it and it is not built.
- **A load-then-undo sequence across a file switch.** `forget()` is reasoned from
  the measured fact that a load discards the stack, not from a run that switches
  files mid-turn and then undoes.

