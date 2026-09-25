"""Empirically answer: what does Ctrl+Z actually revert after Python runs?

!! RUN THIS IN A THROWAWAY Blender SESSION -- it calls undo/redo repeatedly and
!! leaves probe objects behind. Start Blender with `--factory-startup`.

Why this file exists: the recovery design assumed a Python-driven edit could be
walked back with Ctrl+Z. Reading the source says otherwise --
`WM_operator_call_py()` bumps `wm->op_undo_depth`, so operators called from
Python never push an undo step. This probe settles the question by measurement.
It CANNOT run headlessly: undo needs a screen, so there is nothing to see under
`blender -b`, and no agent can run it on your behalf.

How to run:

    /Applications/Blender.app/Contents/MacOS/Blender --factory-startup

    Scripting workspace -> Open -> tools/undo_probe.py -> Run Script

**You do not need the system console.** Every result is also written to

    .scratch/blender-copilot/research/undo-gui-results.txt

which is the file to hand back and which needs no terminal to read. That path is
hard-coded because a script run from the Text Editor has no `__file__` and an
unsaved .blend has no directory to fall back on.

It covers the cases on ticket 15 -- *Confirm the four undo cases in a GUI*:

    1  operator from Python, push at the END, one undo
    2  operator from Python, NO push -- and how many undos it takes to remove it
    3  direct RNA write, with and without a push at the END
    4  a multi-operation turn, ONE push, ONE undo          <- the ticket's case 1
    5  push BEFORE the change                              <- the negative control
    6  Global Undo OFF                                     <- the ticket's case 4
    7  an active edit-mode object                          <- the ticket's case 5

The probe records **raw state transitions, not verdicts**: object sets before and
after, the return value of every undo, and what actually changed. Interpretation
belongs on the ticket, so nothing here is scored PASS or FAIL. Three things no
script can do are listed at the end of the report.
"""

import bpy
import datetime
import os
import sys

REPORT_PATH = (
    "/Users/user/Projects/blender.anx.copilot"
    "/.scratch/blender-copilot/research/undo-gui-results.txt"
)

RESULTS: list[str] = []


def _snap() -> list[str]:
    return sorted(obj.name for obj in bpy.data.objects)


def _rec(case: str, before, action: str, ret, after, note: str = "") -> None:
    """Record one raw observation to the console and to the report file."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    block = (
        f"\n=== {case}\n"
        f"    action   : {action}\n"
        f"    return   : {ret}\n"
        f"    objects  : {len(before)} -> {len(after)}\n"
        f"    added    : {added or '-'}\n"
        f"    removed  : {removed or '-'}\n"
        f"    note     : {note or '-'}\n"
    )
    RESULTS.append(block)
    print("PROBE |" + block.replace("\n", "\nPROBE |"))
    _flush()


def _cube(name: str):
    before = set(bpy.data.objects)
    bpy.ops.mesh.primitive_cube_add()
    obj = (set(bpy.data.objects) - before).pop()
    obj.name = name
    return obj


def _push(label: str) -> str:
    try:
        bpy.ops.ed.undo_push(message=label)
        return f"undo_push({label!r}) -> ok"
    except Exception as exc:
        return f"undo_push({label!r}) -> RAISED {type(exc).__name__}: {exc}"


def _undo() -> str:
    try:
        return repr(bpy.ops.ed.undo())
    except Exception as exc:
        return f"undo -> RAISED {type(exc).__name__}: {exc}"


def _z(name: str):
    obj = bpy.data.objects.get(name)
    return None if obj is None else round(obj.location.z, 3)


def _case(number: str, title: str, body) -> None:
    """Run one case; a crash is recorded rather than aborting the run."""
    try:
        body()
    except Exception as exc:
        _rec(f"{number}  {title}", [], "crashed", "", [],
             f"{type(exc).__name__}: {exc}")


def run_all() -> None:
    prefs_edit = bpy.context.preferences.edit
    print("\nPROBE ==================================================")
    print(f"PROBE blender {bpy.app.version_string}, python {sys.version.split()[0]}")
    print(f"PROBE global undo at start: {prefs_edit.use_global_undo}")
    print(f"PROBE objects at start: {_snap()}")
    print("PROBE ==================================================\n")

    # 1 ---------------------------------------------------------------
    def c1():
        _cube("Probe1")
        b = _snap()
        p = _push("probe: cube added, push at END")
        r = _undo()
        a = _snap()
        _rec("1  operator from Python, push at END, one undo", b,
             f"primitive_cube_add(); {p}; undo", r, a,
             "Probe1 gone means a pushed step CAN revert a Python-driven operator")

    # 2 ---------------------------------------------------------------
    def c2():
        _push("probe: BASELINE for case 2")
        base = _snap()
        _cube("Probe2")
        undos = []
        for _ in range(4):
            if "Probe2" not in bpy.data.objects:
                break
            undos.append(_undo())
        a = _snap()
        _rec("2  operator from Python, NO push", base,
             "undo_push(BASELINE); primitive_cube_add() with NO push; undo until Probe2 is gone",
             f"{len(undos)} undo call(s): {undos}", a,
             "one undo means the add was absorbed into the previous step, i.e. NOT separately revertible")

    # 3 ---------------------------------------------------------------
    def c3a():
        obj = _cube("Probe3a")
        obj.location.z = 0.0
        p1 = _push("probe: marker, Probe3a at z=0")
        base = _snap()
        obj.location.z = 3.0
        z1 = _z("Probe3a")
        p2 = _push("probe: RNA write, push at END")
        r = _undo()
        a = _snap()
        _rec("3a direct RNA write, push at END", base,
             f"Probe3a.location.z 0.0 -> 3.0; {p2}; undo", r, a,
             f"z 0.0 -> {z1} -> {_z('Probe3a')} with cube present={('Probe3a' in bpy.data.objects)}"
             f"  ({p1})")

    def c3b():
        obj = _cube("Probe3b")
        obj.location.z = 0.0
        _push("probe: marker, Probe3b at z=0")
        base = _snap()
        obj.location.z = 3.0
        z1 = _z("Probe3b")
        r = _undo()
        a = _snap()
        _rec("3b direct RNA write, NO push", base,
             "Probe3b.location.z 0.0 -> 3.0; then undo", r, a,
             f"z 0.0 -> {z1} -> {_z('Probe3b')} with cube present={('Probe3b' in bpy.data.objects)}"
             "  -- if it jumped past the z=0 marker, the RNA write was never a step")

    # 4 ---------------------------------------------------------------
    def c4():
        _push("probe: BASELINE for case 4")
        base = _snap()
        o1 = _cube("Probe4a")
        o2 = _cube("Probe4b")
        o1.location.z = 2.0
        o2.scale = (2.0, 2.0, 2.0)
        p = _push("COPILOT TURN 7")
        r = _undo()
        a = _snap()
        _rec("4  multi-op turn, ONE push, ONE undo", base,
             f"two cube_add + an RNA move + an RNA scale, then ONE {p}, then ONE undo", r, a,
             "both Probe4a and Probe4b gone in a single undo means push-at-END covers a whole turn")

    # 5 ---------------------------------------------------------------
    def c5():
        obj = _cube("Probe5")
        obj.location.z = 0.0
        p = _push("probe: marker BEFORE the change")
        before = _snap()
        obj.location.z = 5.0
        r = _undo()
        a = _snap()
        _rec("5  push BEFORE the change (negative control)", before,
             f"set z=0.0, {p}, set z=5.0, then undo", r, a,
             f"z 0.0 -> 5.0 -> {_z('Probe5')}; expect 0.0, and a vanished cube means it reverted too far")

    # 6 ---------------------------------------------------------------
    def c6():
        prefs_edit.use_global_undo = False
        try:
            _cube("Probe6")
            p = _push("probe: global-undo-off marker")
            before = _snap()
            bpy.data.objects["Probe6"].location.z = 7.0
            r = _undo()
            a = _snap()
            _rec("6  Global Undo OFF", before,
                 f"Global Undo off; cube added; {p}; z set to 7.0; undo", r, a,
                 f"z={_z('Probe6')}, cube present={('Probe6' in bpy.data.objects)}"
                 "  -- if nothing changed, recoverability is genuinely gone and the"
                 " preference is a valid detector")
        finally:
            prefs_edit.use_global_undo = True

    # 7 ---------------------------------------------------------------
    def c7():
        obj = _cube("Probe7")
        bpy.context.view_layer.objects.active = obj
        before = _snap()
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            # mode_set reloads the object's data, invalidating any reference taken
            # before the switch. Re-resolve instead of trusting the old `obj`;
            # a stale reference here raises ReferenceError: StructRNA removed.
            obj = bpy.context.view_layer.objects.active
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts[0].co.z += 1.0
            bmesh.update_edit_mesh(obj.data)
            mode_in = bpy.context.mode
            p = _push("probe: edit-mode vertex move")
            z_moved = round(obj.data.vertices[0].co.z, 3)
            r = _undo()
            live = bpy.data.objects.get("Probe7")
            z_after = (
                "cube gone after undo"
                if live is None
                else round(live.data.vertices[0].co.z, 3)
            )
            _rec("7  active edit-mode object", before,
                 f"enter EDIT, move one vertex by +1.0, {p}, undo", r, _snap(),
                 f"mode was {mode_in} (now {bpy.context.mode}); vertex z 1.0 -> {z_moved} -> {z_after}"
                 "  -- edit mode keeps its own undo stack, so this is best-effort")
        finally:
            try:
                if bpy.context.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
            except Exception as exc:
                print(f"PROBE | could not leave edit mode: {exc}")

    all_cases = (
        ("1", "operator from Python, push at END", c1),
        ("2", "operator from Python, NO push", c2),
        ("3a", "direct RNA write, push at END", c3a),
        ("3b", "direct RNA write, NO push", c3b),
        ("4", "multi-op turn, ONE push", c4),
        ("5", "push BEFORE the change", c5),
        ("6", "Global Undo OFF", c6),
        ("7", "active edit-mode object", c7),
    )
    # In a fresh Blender the cases interfere: each undo moves the stack pointer,
    # and toggling Global Undo invalidates object references. To re-measure one
    # case cleanly, list it in BC_UNDO_CASE, e.g.  BC_UNDO_CASE=6,7
    only = {c.strip() for c in os.environ.get("BC_UNDO_CASE", "").split(",") if c.strip()}
    if only:
        print(f"PROBE running only case(s): {sorted(only)}\n")
    for number, title, body in all_cases:
        if only and number not in only:
            continue
        _case(number, title, body)

    _write_report()


_HUMAN = (
    "THREE THINGS NO SCRIPT CAN DO -- please check these and report them:\n"
    "\n"
    "  A. Press the REAL Ctrl+Z key (not bpy.ops.ed.undo) after the run and\n"
    "     confirm it behaves as the programmatic undo above did. The addon's\n"
    "     stop/undo path goes through the keypress, not the operator.\n"
    "  B. Open Edit > Undo History and read the step labels. Do the steps\n"
    "     pushed above appear, and does 'COPILOT TURN 7' appear as ONE step?\n"
    "  C. Count the steps in Undo History and compare with the number of\n"
    "     undo_push calls this run made. A mismatch means pushes are being\n"
    "     merged or dropped.\n"
)


def _header() -> str:
    return (
        "Undo probe -- raw observations from a GUI session\n"
        "Ticket: .scratch/blender-copilot/issues/15-undo-gui-confirmation.md\n"
        f"Blender {bpy.app.version_string}, python {sys.version.split()[0]}\n"
        f"when: {datetime.datetime.now().isoformat(timespec='seconds')}\n"
        f"global undo at end: {bpy.context.preferences.edit.use_global_undo}\n\n"
        "These are raw state transitions, not verdicts. Interpretation belongs\n"
        "on the ticket, so nothing here is scored PASS or FAIL.\n"
    )


def _flush() -> None:
    """Write what we have so far.

    Called after every case, because this file is the only record of a single
    manual run: if a later case crashes or hangs, the earlier observations are
    already on disk.
    """
    text = _header() + "".join(RESULTS) + "\n" + _HUMAN
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception as exc:
        print(f"PROBE | could not write {REPORT_PATH}: {exc}")


def _write_report() -> None:
    _flush()
    print(f"\nPROBE wrote {REPORT_PATH}\n")
    print(_HUMAN)


if os.environ.get("BC_QUIT"):
    # Launched non-headlessly from a terminal with --python, so this script body
    # runs *before* the event loop starts. Undo needs a screen, so defer to a
    # timer and quit from inside it. Run it under the bounded driver anyway:
    #   python3 tools/bounded_run.py 60 -- env BC_QUIT=1 \
    #     /Applications/Blender.app/Contents/MacOS/Blender \
    #     --factory-startup --python tools/undo_probe.py
    def _deferred() -> None:
        try:
            run_all()
        finally:
            bpy.ops.wm.quit_blender()

    bpy.app.timers.register(_deferred, first_interval=1.0)
else:
    run_all()
