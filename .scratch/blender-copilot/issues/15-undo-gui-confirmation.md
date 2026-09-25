# Confirm the four undo cases in a GUI

> **DO NOT CLAIM. This ticket requires a human sitting at a GUI.** It is the one
> item in the frontier that no instance can resolve — the protocol forbids
> launching a GUI application, and undo needs a screen that `--background` does
> not have. It is listed as a ticket because the work is real and currently
> *unverified*, not because it is queued for an agent. Do not pass `15` to
> `tools/wayfinder-wave.sh`.

Type: task
Status: resolved
Blocked by: none

## Question

**This ticket requires a human at a GUI. No agent can resolve it** — the
protocol forbids launching a GUI application or taking screenshots, and undo
requires a screen (`--background` has none). It is recorded here because the
whole recovery design rests on it and it is currently *unverified*, not because
an instance should claim it.

*Build the cheapest installable extension that proves the panel* built
`tools/undo_probe.py` and then handed the GUI run over rather than automating it.
*What replaces undo as the recovery mechanism?* confirmed that the four-case run
is still outstanding and that every undo claim in the map rests on
`--background` probes plus the live-GUI timer probes from *What exactly happens
when we exec model code, and what does undo cover?*.

Run `tools/undo_probe.py` in the installed Blender 5.2.2 and record, with the
observed behaviour rather than a prediction:

1. **Push at the end reverts exactly the turn.** Run a turn that mutates
   `bpy.data` in several operator calls, then Ctrl+Z once: does the scene return
   to its pre-turn state in a single step, and is the step labelled with the
   turn?
2. **Push at the start reverts too far** — the negative control that justifies
   the push-at-end rule. If this case behaves differently than predicted, the
   whole push discipline is wrong and *What replaces undo as the recovery
   mechanism?* §1 must be rewritten.
3. **Direct RNA writes and Python-called operators are covered** by the pushed
   step, i.e. the earlier `--background` finding that they push nothing holds for
   the GUI path too.
4. **Global Undo off** genuinely removes recoverability, and the addon's
   detection (`preferences.edit.use_global_undo`) matches what actually happens
   to Ctrl+Z in that state. This is what justifies pausing auto-run.

Also worth capturing while a human is present, because no probe has covered it:
whether an **active edit-mode object** behaves like Global-Undo-off
(`ED_undo_is_memfile_compatible` declines, so a push does not snapshot the
scene). *What replaces undo as the recovery mechanism?* §4 states this pending a
probe and calls it the item most likely to be wrong.

Deliverable: the four (plus optional fifth) observed results, by case, each
marked verified-and-how. If any case contradicts a decision in *What replaces
undo as the recovery mechanism?*, say so plainly and file the amendment — do not
soften the result to fit the design.

## Answer

**Measured in the installed Blender 5.2.2, 2026-09-26. All five cases ran.**
Raw transitions: `research/undo-gui-results-run1.txt` (the first run, all cases)
and `research/undo-gui-results-run2-both.txt`, `-run3-case7.txt` and
`undo-gui-results.txt` (isolated re-runs). **Each undo moves the stack pointer,
so the cases interfere when run together — the isolated runs are the trustworthy
ones, and one of them overturned my first reading.**

Probe: `tools/undo_probe.py`. The first four cases were run by hand in the GUI;
the last two by the orchestrator as bounded non-headless runs
(`python3 tools/bounded_run.py 60 -- env BC_QUIT=1 BC_UNDO_CASE=… <blender>
--factory-startup --python tools/undo_probe.py`) after the owner broadened the GUI
permission from one ticket to "cases that genuinely need a screen".

### The four cases

**1. Push at the END reverts exactly the turn — CONFIRMED.** Case 4: a turn of
four operations (two `primitive_cube_add`, an RNA move, an RNA scale), **one**
push labelled `COPILOT TURN 7`, **one** undo. The object set returned to the
pre-turn snapshot exactly — `4 -> 4`, both cubes gone, nothing else touched.
Cases 1 and 3a reproduce it for a single operator and for a direct RNA write, the
write case reverting `z 3.0 -> 0.0` with the object intact. This is *What replaces
undo as the recovery mechanism?* §1's claim, until now reasoned only from source.

**2. Push at the START reverts too far — CONFIRMED, exactly as predicted.**
Case 5, the negative control: cube at `z=0`, push, then `z=5.0`, then one undo →
**the cube is gone**, not `z` back to `0.0`. The undo lands on the step *before*
the push, so the change it was meant to protect is not merely unprotected but
unreachable. The push-at-END rule survives the test designed to break it.

**3. RNA writes and Python-called operators are covered by the pushed step —
CONFIRMED.** With a push, reverted exactly (case 3a). Without one the contrast is
worse than "no undo": case 3b jumped *past* its `z=0` marker and deleted the
object, and case 2 absorbed an operator's effect into the previous step. An
unpushed change does not merely fail to be revertible — **Ctrl+Z reaches further
back than it**, into work the user was not touching.

**4. Global Undo off — the decision STANDS, but my first reading was wrong and
the correction makes it stronger.** Run 1 put this case after five others, where
it showed the undo returning `FINISHED` and removing a cube — apparently "undo
still works". **That was contaminated.** Run in a fresh session, and reproduced
twice, the honest result is:

```
undo -> RAISED RuntimeError: Operator bpy.ops.ed.undo.poll() failed, context is incorrect
objects 4 -> 4 | z stayed 7.0 | cube still present
```

So with Global Undo off, **`bpy.ops.ed.undo()` fails its poll and raises; nothing
is reverted.** The control that makes this attributable: in the *same* run, in the
*same* timer context, case 7's undo returned `{'FINISHED'}` once the preference
was restored. Same context, different preference, different outcome — so the
failure is the preference, not the context. Two consequences for §4, both
improvements: `preferences.edit.use_global_undo` **is** a valid detector, and
there is a second, mechanical one — the addon need not *read* the preference to
know undo is dead, it can attempt the push and catch the poll failure.

### The optional fifth case, which is the one that bites

**5. An active edit-mode object — CONFIRMED, and worse than predicted.**

```
undo_push('probe: edit-mode vertex move') -> ok
undo -> {'FINISHED'}
objects 4 -> 3, removed: ['Probe7']
mode was EDIT_MESH (now OBJECT); the cube is gone
```

§4 predicted this from `ED_undo_is_memfile_compatible` declining and called it
"the item most likely to be wrong". It was right. The push **returns ok and
records nothing usable**; the following undo rolls back past the object entirely
and takes it with it. So in edit mode the addon has no way to create a revertible
step, and a push there produces a Ctrl+Z that **deletes the object the user was
editing** — worse than not pushing at all.

**Design consequence for §4 to absorb:** edit mode must be treated exactly like
Global Undo off — auto-run pauses — and pushing while in edit mode should be
**refused**, not attempted. Whether the addon may instead exit edit mode before
pushing is not settled here, because exiting edit mode is itself a user-visible
state change.

### The three things no script could do

- **A. The real Ctrl+Z keypress — CONFIRMED.** Observed by the owner as behaving
  as the programmatic undo did. The keypress path and the operator path agree, so
  the addon's stop/undo path is not a special case.
- **B. Step labels — CONFIRMED.** Undo History shows the `undo_push(message=…)`
  string verbatim, e.g. *"probe: marker, Probe3a at z=0"*. A turn can be labelled
  and the user can find which step to revert. (`COPILOT TURN 7` was absent at the
  end of run 1 because the probe had undone past it — Blender's history menu lists
  steps *behind* the pointer, not the discarded redo branch.)
- **C. Step count — inconclusive, recorded rather than guessed.** Blender's own
  entries sit in the same history (the screenshot shows *"Open Text"* and
  *"Select"*), and the probe's undos move the pointer, so a raw count does not
  isolate the pushes.

### One collision found while reading the screenshot

Run 1's Undo History contains **"Open Text"** — opening the probe from the Text
Editor created an undo step. *How a conversation is laid out and controlled* binds
the code companion and the full-history escape hatch to an addon-owned `Text`
datablock, so **the addon's own UI actions will appear in the same undo history
the user reads to find a turn.** That belongs on that ticket.

### What this does not establish

- Whether an edit-mode push can be made usable by exiting edit mode first.
- Whether a pushed state survives `mode_set` in the other direction
  (object mode → edit mode).
- Nothing here rests on a screenshot of a *result*; the only screenshot is of the
  Undo History menu, and it is the human's, not an agent's.

## Comments
