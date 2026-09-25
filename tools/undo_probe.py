"""Empirically answer: what does Ctrl+Z actually revert after Python runs?

!! Run this in a THROWAWAY Blender session with an empty scene. !!
It calls undo/redo repeatedly and will disturb anything you care about.

Why: the map's recovery design assumed a Python-driven edit could be walked back
with Ctrl+Z. Reading the source says otherwise -- `WM_operator_call_py()` bumps
`wm->op_undo_depth`, so operators called from Python never push an undo step.
This probe settles it by measurement instead of argument, and it has to be run
in a GUI session because undo needs a screen and does nothing under `-b`.

How to run: Blender -> Text Editor -> open this file -> Run Script.
Read the results in the system console (macOS: run Blender from a terminal, or
Window -> Toggle System Console on other platforms).
"""

import bpy


def _object_names() -> set[str]:
    return {obj.name for obj in bpy.data.objects}


def _push(label: str) -> None:
    bpy.ops.ed.undo_push(message=label)


def _report(label: str, verdict: str) -> None:
    print(f"PROBE | {label:<46} | {verdict}")


def probe() -> None:
    print("\nPROBE ------------------------------------------------")
    print(f"PROBE global undo enabled: {bpy.context.preferences.edit.use_global_undo}")
    print(f"PROBE objects at start: {len(bpy.data.objects)}")

    # ---- Case 1: an operator called from Python -------------------------
    before = _object_names()
    bpy.ops.mesh.primitive_cube_add()
    added = _object_names() - before
    _push("probe: cube added")
    bpy.ops.ed.undo()
    survived = added & _object_names()
    _report(
        "operator from Python, push AFTER, then undo",
        f"cube removed again: {not survived} (added={sorted(added)})",
    )

    # ---- Case 2: an operator from Python with no push at all ------------
    before = _object_names()
    bpy.ops.mesh.primitive_cube_add()
    added = _object_names() - before
    bpy.ops.ed.undo()
    survived = added & _object_names()
    _report(
        "operator from Python, NO push, then undo",
        f"cube removed again: {not survived} (added={sorted(added)})"
        + ("" if not survived else "  <-- NOT undoable on its own"),
    )

    # ---- Case 3: a direct RNA write -------------------------------------
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    start_z = obj.location.z
    obj.location.z += 1.0
    moved = obj.location.z
    _push("probe: RNA write")
    bpy.ops.ed.undo()
    after_undo = bpy.data.objects[obj.name].location.z if obj.name in bpy.data.objects else None
    _report(
        "direct RNA write, push AFTER, then undo",
        f"z {start_z:.2f} -> {moved:.2f} -> {after_undo}",
    )

    # ---- Case 4: push BEFORE the change --------------------------------
    obj.location.z = 0.0
    _push("probe: marker before change")
    obj.location.z = 5.0
    bpy.ops.ed.undo()
    after_undo = bpy.data.objects[obj.name].location.z if obj.name in bpy.data.objects else None
    _report(
        "push BEFORE the change, then undo",
        f"z 0.00 -> 5.00 -> {after_undo}  (expect 0.00 if push-at-end is right)",
    )

    print("PROBE ------------------------------------------------\n")
    print(
        "PROBE Interpretation: if case 2 shows the cube still present, a Python-driven\n"
        "PROBE edit is NOT undoable unless we push explicitly, and the push belongs at\n"
        "PROBE the END of the unit being reverted. Report the four lines above on the\n"
        "PROBE ticket 'What replaces undo as the recovery mechanism?'.\n"
    )


probe()
