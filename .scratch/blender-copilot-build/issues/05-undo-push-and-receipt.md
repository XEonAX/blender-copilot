# 05: The undo push and the receipt

**What to build:** a change the agent made can be taken back with one Ctrl+Z, and
the panel tells the user so. **This is the ticket that can corrupt a real file** —
everything else can be re-run, this cannot — so it is the one that keeps the heavy
verification.

**Blocked by:** 02 (One tool call, end to end), 04 (Persistence)

**Status:** open
**Triage:** ready-for-agent

- [ ] After a turn that changed the scene, one Ctrl+Z reverts exactly that turn — not the previous one, and not two turns.
- [ ] The step is labelled with the turn, and the label appears verbatim in Blender's undo history.
- [ ] A turn that changed nothing does not push.
- [ ] The push happens at the **end** of the turn, in a `finally`, so an interrupted turn still leaves a revertible step.
- [ ] Pushing is **refused** while an object is in edit mode, not attempted.
- [ ] The panel shows a receipt naming what changed, plus the shortcut for Blender's undo history.
- [ ] With Global Undo off, auto-run pauses and the panel says so.
- [ ] The receipt is honest that undoing covers local scene data only — not files, not network, not preferences.

**Context:** *What replaces undo as the recovery mechanism?* fixes the push
discipline, and *Confirm the four undo cases in a GUI* **measured** it — pushing at
the end works, one push covers a multi-operation turn, pushing before the change
reverts too far, Global Undo off makes undo raise rather than no-op, and a push
taken in edit mode records nothing usable and the undo after it **deletes the
object**. Those are measurements, so treat them as constraints rather than
hypotheses.
