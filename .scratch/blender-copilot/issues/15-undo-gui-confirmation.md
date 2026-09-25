# Confirm the four undo cases in a GUI

> **DO NOT CLAIM. This ticket requires a human sitting at a GUI.** It is the one
> item in the frontier that no instance can resolve — the protocol forbids
> launching a GUI application, and undo needs a screen that `--background` does
> not have. It is listed as a ticket because the work is real and currently
> *unverified*, not because it is queued for an agent. Do not pass `15` to
> `tools/wayfinder-wave.sh`.

Type: task
Status: open
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

<!-- recorded on resolution; not written at chart time -->

## Comments
