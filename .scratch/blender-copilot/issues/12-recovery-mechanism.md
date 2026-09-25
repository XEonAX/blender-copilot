# What replaces undo as the recovery mechanism?

Type: grilling
Status: open
Blocked by: none

## Question

Undo turned out to be much weaker than the map assumed, while the design still auto-runs arbitrary code with no approval gate. Rebuild the recovery design around what undo actually does.

Established: Python-initiated operators never push undo; direct RNA writes push nothing; undo reaches only local `bpy.data`, only after an explicit push, and the push belongs at the **end** of the unit being reverted; undo never reaches filesystem writes or deletes, subprocesses, network calls, render/GPU state, screens/workspaces/WM, preferences, Python state, or linked-library data; and it is absent entirely when Global Undo is off.

Decide:

1. **Push discipline.** Per tool call, or per turn? What exact unit does Ctrl+Z revert, and is that the unit the user expects? Note that the push-at-end semantics change the answer the map originally assumed.
2. **Snapshot cadence.** Is `wm.save_as_mainfile(copy=True)` really the mechanism? When is it taken, what does it cost on a large scene, where does it live, and is it a genuine backstop or theatre?
3. **A before-image.** Should the addon record a cheap record of what the agent is about to touch, so it can *report* what changed given there is no reliable undo? What is the cheapest sufficient representation — `get_scene_info` output, an RNA path list, something else?
4. **Global Undo off.** How is that detected, and should the addon refuse to auto-run arbitrary code when it is, or warn loudly and proceed?
5. **Honesty in the UI.** What does the panel claim about what is *not* recoverable? A blanket "everything is undoable" is a lie; a warning on every run is noise. Find the true sentence.
6. **Does auto-run survive this?** If the honest conclusion is that no recovery mechanism can cover arbitrary code, say so plainly — and then say what that does to the no-approval-gate decision, which was made before these facts were known.

Read *What exactly happens when we exec model code, and what does undo cover?* first; it owns the facts. Fold in the empirical undo result the panel prototype is asked to confirm.

Deliverable: the recovery design, the push and snapshot rules, and the exact user-facing promise.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
