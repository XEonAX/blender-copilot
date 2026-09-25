# Whose state is it when more than one panel is open

Type: grilling
Status: open
Blocked by: none

## Question

The map has carried "what happens with more than one panel open — several windows
or areas showing the same conversation, and whether the agent's state is per-panel
or per-process" as fog since charting. Two tickets have since made it sharp
enough to answer.

*How a conversation is laid out and controlled* made the panel stateful and gave
it a `boxes` layout with paging, a busy kind, and a Stop control backed by a
subprocess handle. *How a conversation degrades as context grows* then split the
world cleanly in two: the **store** (the user's record, one file per conversation,
lossless) and the **projection** (the degraded view sent to the model, derived at
request time, never written back). *Where chat history lives* scopes the store per
`.blend` path.

Nothing decides who owns the projection, the in-flight request, or the subprocess
when a second panel opens. Decide:

1. **The ownership rule.** Per-panel, per-conversation, or per-process. Name what
   actually breaks in the other two cases rather than asserting a preference:
   what happens to an in-flight turn, the Stop button's target, the undo push in
   *What replaces undo as the recovery mechanism?* §1, and the `Text` datablock
   that *How a conversation is laid out and controlled* binds the code companion
   to.
2. **Two panels, one conversation.** Is it a shared live view, two independent
   scroll positions over one store, or refused outright? The panel cannot scroll
   and pages instead (*What can a Blender 5.2 panel actually render and accept?*),
   so "two views of one store" has a concrete meaning here and is not free.
3. **Two panels, two conversations.** Permitted? If yes, does a second concurrent
   agent run, or does the second panel attach to the running one? Note this is the
   case that makes the global-undo interaction in *What replaces undo as the
   recovery mechanism?* ambiguous: one push per turn presumes one turn at a time.
4. **The subprocess.** *How the addon talks to the API* chose one `_worker.py`
   child per... nothing said. Per panel is one HTTP client per panel; per process
   is a shared client with a queue. Pick, and say what happens to the other
   panel's stream when one panel's Stop kills the child — the failure mode the
   ticket names as `proc.kill()` being terminal.
5. **The cheap answer, if it is defensible.** Given the slice's target, "one panel
   at a time; opening a second attaches or refuses" may be the right call and
   costs nothing to build. If you take it, say what the panel shows in the second
   area, and check whether Blender makes refusal even possible — an addon cannot
   register a space, and the panel is a sidebar in every 3D Viewport, so a second
   instance may not be preventable at all. If it is not preventable, this becomes
   a *must handle* rather than a *may choose*.

Deliverable: the ownership rule, the behaviour for each of the four cases, and
the amendment to *Where chat history lives* or *How the addon talks to the API* if
the answer changes what they own.

## Answer

<!-- recorded on resolution; not written at chart time -->
