#!/usr/bin/env python3
"""Does one Ctrl+Z take back exactly one agent turn?

    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/undo_step_probe.py

Run the same command **without** `--background` to run every case with a window
and take a screenshot of the receipt in the panel on the way through.

**The undo is real; the wire is faked.** Everything that decides whether a step
exists is the shipped code - the loop, the sandbox, `undo_blender`, and
`bpy.ops.ed.undo_push` itself - and every claim below is measured against
Blender's own undo stack with `bpy.ops.ed.undo()`, which is the operator Ctrl+Z
invokes (the keypress and the operator agree, observed by the owner, ticket 15
case A). The only fake is the transport: a scripted worker stands in for
`transport.Worker`, so no request goes anywhere and no credential is read.

Why this can run headless, which ticket 15 expected it could not: measured
2026-09-26 on the installed 5.2.2, `blender -b --factory-startup` **has** a window
and a screen (`Window`, `Screen("Layout")`), so `ed.undo_push` and `ed.undo` pass
their `ED_operator_screenactive` polls and the stack behaves. The one difference
from a GUI session is that a background stack starts life empty and is created by
the first push - which is precisely what ticket 12 §1's baseline guard exists for,
so the guard is exercised here rather than assumed.

What each case is evidence for:

    1  one push, one undo, exactly the turn                                  box 1
    2  the step is labelled with the turn, and that label reaches the push   box 2
    3  a turn that changed nothing does not push                             box 3
    4  a turn stopped mid-flight still leaves a revertible step              box 4
    5  a turn that ends in edit mode refuses the push, rather than making it  box 5
    6  with Global Undo off, Send is refused                              box 7
    7  CONTROL: with Global Undo off the push still "succeeds" and the undo
       raises - so the preference is the ONLY detector (ticket 12's addendum
       claimed a second, mechanical one; this is where that claim fails)
    8  CONTROL: what case 5's refusal prevented - a push in edit mode, then one
       undo, and the object being edited is deleted

It writes `logs/undo-step.txt`, prints a token, and quits Blender itself when it
has a window, so the bounded driver's deadline is a backstop and not the
mechanism.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import bpy

# Dummy values so `transport.config()` is satisfied. The worker is replaced below,
# so nothing is ever sent and no real credential is read: `.env` is not loaded and
# the key here is a literal fake.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "undo-step.txt"
SHOT = ROOT / "logs" / "undo-step.png"

# The probes' conversations must not land in the user's real extension directory.
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-undo-probe")

NOTES: list[str] = []
FAILURES: list[str] = []

# Filled in by `main`, because they only exist once Blender has loaded the
# extension. Module-level so the cases can read them without threading a context
# through every one of them.
bc = None
worker = None
settings = None
session = None
push_labels: list[str] = []


def note(line: str) -> None:
    NOTES.append(line)
    print(f"UNDO | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


# ---------------------------------------------------------------------------
# The scripted transport: the worker protocol, no pipe and no HTTP.
# ---------------------------------------------------------------------------

def tool_round(call_id: str, purpose: str, code: str, prose: str = "") -> list[dict]:
    events: list[dict] = []
    if prose:
        events.append({"ev": "delta", "text": prose})
    events.append(
        {
            "ev": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_blender_python",
                        "arguments": json.dumps({"code": code, "purpose": purpose}),
                    },
                }
            ],
        }
    )
    return events


def multi_tool_round(calls: list[tuple[str, str, str]]) -> list[dict]:
    """One reply asking for several calls, which is what Stop needs."""
    return [
        {
            "ev": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_blender_python",
                        "arguments": json.dumps({"code": code, "purpose": purpose}),
                    },
                }
                for call_id, purpose, code in calls
            ],
        }
    ]


def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


class ScriptedWorker:
    """Stands in for `transport.Worker`: same methods, no pipe and no HTTP."""

    def __init__(self) -> None:
        self.rounds: list[list[dict]] = []
        self.sent: list[dict] = []
        self._queue: list[dict] = []

    @property
    def busy(self) -> bool:
        return bool(self._queue or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.sent.append({"messages": messages, "tools": tools})
        self._queue.extend(self.rounds.pop(0) if self.rounds else [])
        return None

    def tick(self) -> list[dict]:
        events, self._queue = self._queue, []
        return events

    def cancel(self) -> None:
        self._queue = []
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Driving the shipped loop, the way its own timer does
# ---------------------------------------------------------------------------

def drive(session, ticks: int = 60) -> bool:
    """Tick until the turn is over. True when it ended inside the budget.

    `stream._tick` is the function `bpy.app.timers` calls; calling it in a loop is
    what a timer does, and it is the only option under `-b`, where timers never
    fire. `stream.start()` is undone after each send so a GUI run cannot also be
    driven by the real timer and race this one.
    """
    for _ in range(ticks):
        bc.stream._tick()
        if not session.streaming:
            return True
    return False


def send(prompt: str, rounds: list[list[dict]]) -> str:
    """One real `Send` through the real operator, with a scripted reply."""
    worker.rounds = list(rounds)
    settings.prompt_text = prompt
    result = bpy.ops.blender_copilot.send()
    bc.stream.stop()
    note(f"send({prompt!r}) -> {result} with {len(rounds)} scripted round(s)")
    return "FINISHED" if "FINISHED" in result else ""


def turn(prompt: str, rounds: list[list[dict]]) -> bool:
    """Send, drive, and report whether the turn ended."""
    if not send(prompt, rounds):
        FAILURES.append(f"Send refused for {prompt!r}")
        return False
    if not drive(session):
        FAILURES.append(f"the turn for {prompt!r} did not end")
        note(f"turn for {prompt!r} stuck at {session.status!r}")
        return False
    return True


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def scale_z() -> float | None:
    cube = bpy.data.objects.get("Cube")
    return None if cube is None else round(cube.scale.z, 4)


def object_names() -> list[str]:
    return sorted(obj.name for obj in bpy.data.objects)


def undo_once() -> str:
    """One Ctrl+Z, as the user's keypress reaches it."""
    try:
        return repr(bpy.ops.ed.undo())
    except BaseException as exc:  # noqa: BLE001 - a refusal is a result
        return f"RAISED {type(exc).__name__}: {exc}"


def enter_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------

def case_1(session) -> None:
    """One push, one undo, exactly the turn (box 1), labelled with it (box 2)."""
    note(f"--- case 1: a mutating turn, one undo")
    note(f"cube scale.z before: {scale_z()}")
    code = (
        "obj = C.active_object\n"
        "obj.scale.z = 1.5\n"
        "C.view_layer.update()\n"
        "print(obj.name, obj.scale.z)"
    )
    if not turn("make the cube taller", [tool_round("c1", "Scale Cube 1.5x on Z", code), prose_round("Done.")]):
        return
    note(f"cube scale.z after: {scale_z()}")
    check("the code ran and the cube really changed", scale_z() == 1.5)

    receipt = session.last_receipt
    check("the turn left a receipt", isinstance(receipt, dict))
    if not isinstance(receipt, dict):
        return
    check("and the receipt says the turn is undoable", receipt["undoable"] is True)
    check(
        "the receipt is honest that the bounded summary cannot name this change",
        receipt["changed"] == [bc.undo.UNNAMED_CHANGE],
    )
    check(
        "the step is labelled with the turn",
        receipt["label"] == "copilot: make the cube taller",
    )
    check(
        "the label is what the push was called with",
        push_labels == [bc.undo.BASELINE_LABEL, "copilot: make the cube taller"],
    )
    note(f"pushes so far: {push_labels}")

    before_undo = object_names()
    result = undo_once()
    note(f"one undo -> {result}")
    check("one undo reverts the turn", scale_z() == 1.0)
    check("and touched nothing else", object_names() == before_undo)
    check("the receipt still names the step it created", session.last_receipt is not None)


def case_2(session) -> None:
    """A change the summary CAN name, so the receipt has something to say."""
    note("--- case 2: a named change")
    code = (
        "target = D.objects.new('Target', None)\n"
        "C.scene.collection.objects.link(target)\n"
        "C.view_layer.update()\n"
        "print('linked', target.name)"
    )
    if not turn("add an empty target marker", [tool_round("c2", "Add Empty Target", code), prose_round("Added.")]):
        return
    receipt = session.last_receipt or {}
    note(f"receipt.changed = {receipt.get('changed')}")
    check("objects count is named", any(line.startswith("objects:") for line in receipt.get("changed", [])))
    check("the new Empty type is named", any("EMPTY" in line for line in receipt.get("changed", [])))
    check("and the receipt is undoable", receipt.get("undoable") is True)
    # The honesty limit, checked rather than assumed: the summary is
    # datablock-level, so the receipt names counts and types, never the object the
    # agent just created.
    check(
        "and it does not pretend to name the object, because the summary cannot",
        not any("Target" in line for line in receipt.get("changed", [])),
    )
    check(
        "the coverage sentence rides on every receipt",
        any("not files, network" in line for line in receipt.get("lines", [])),
    )
    check("Target is in the scene", "Target" in object_names())


def case_3(session) -> None:
    """A turn that changed nothing does not push (box 3).

    Two claims, and the second is the one that cannot be faked: no push appears,
    *and* the next Ctrl+Z skips this turn entirely and reverts case 2. If the
    read-only turn had pushed, one undo would have landed on its own snapshot -
    the same scene, nothing visibly reverted - and it would have taken two.
    """
    note("--- case 3: a turn that changed nothing")
    pushes_before = list(push_labels)
    code = "print('scale.z is', C.active_object.scale.z)"
    if not turn("what is the cube's scale", [tool_round("c3", "Read Cube scale", code), prose_round("1.0.")]):
        return
    check("no undo step was pushed", push_labels == pushes_before)
    check("and the panel shows no receipt", session.last_receipt is None)
    rows = [message for message in session.messages if message.kind == "tool"]
    check(
        "the read-only call really ran and reported ok",
        bool(rows) and rows[-1].status == "ok",
    )

    result = undo_once()
    note(f"one undo after the read-only turn -> {result}")
    check("one undo skips the turn that changed nothing", "Target" not in object_names())
    note(f"objects now: {object_names()}")


def case_4(session) -> None:
    """An interrupted turn still leaves a revertible step (box 4).

    The interruption is the real one: a reply asking for two calls, the first of
    which mutates, then the panel's own **Stop** while the second is still queued.
    Stop ends the turn inside the operator that handled the click, which is
    exactly why the step cannot be written by a tick that watches the transition -
    there is no such tick.
    """
    note("--- case 4: Stop mid-turn")
    scale_before = scale_z()
    note(f"scale.z before: {scale_before}")
    rounds = [
        multi_tool_round(
            [
                ("c4a", "Scale it to 2.0", "C.active_object.scale.z = 2.0\nC.view_layer.update()\nprint('scaled')"),
                ("c4b", "Scale it again", "C.active_object.scale.z = 3.0\nC.view_layer.update()"),
            ]
        ),
        prose_round("Both done."),
    ]
    if not send("make it much taller", rounds):
        return
    # Tick until the first call has run and the second is still queued: that is the
    # moment a user would press Stop.
    queued = False
    for _ in range(20):
        bc.stream._tick()
        if len(session.pending) == 1:
            queued = True
            break
    check("the second call is still queued when Stop is pressed", queued)
    note(f"stopping with {len(session.pending)} call(s) queued")
    result = bpy.ops.blender_copilot.stop()
    bc.stream.stop()
    note(f"stop -> {result}")
    check("the turn is over", not session.streaming)
    # Stop in the tool phase answers the queued call rather than leaving it
    # `running…`, which is the loop's own rule and not this probe's business - but
    # it is the state a step has to be written from.
    rows = [message for message in session.messages if message.kind == "tool"]
    check("the call that never ran is answered, not left running", bool(rows) and rows[-1].status == "error")
    check("the mutation the first call made is still there", scale_z() == 2.0)

    receipt = session.last_receipt
    check("an interrupted turn still gets a receipt", isinstance(receipt, dict))
    if isinstance(receipt, dict):
        check("and it says the partial turn is undoable", receipt["undoable"] is True)
        check(
            "labelled with the turn that was interrupted",
            receipt["label"] == "copilot: make it much taller",
        )
    note(f"pushes so far: {push_labels}")
    result = undo_once()
    note(f"one undo of the interrupted turn -> {result}")
    check("one undo reverts the partial turn", scale_z() == scale_before)


def case_5(session) -> None:
    """A turn that ends in edit mode refuses the push (box 5)."""
    note("--- case 5: a turn that ends in edit mode")
    ready = session.streaming is False
    check("the session is idle before the case", ready)
    bpy.ops.object.select_all(action="SELECT")
    bpy.data.objects["Cube"].select_set(True)
    bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
    enter_object_mode()
    pushes_before = list(push_labels)
    code = "bpy.ops.object.mode_set(mode='EDIT')\nprint('mode is', C.mode)"
    if not turn("start editing the cube", [tool_round("c5", "Enter edit mode", code), prose_round("Editing.")]):
        return
    note(f"mode after the turn: {bpy.context.mode}")
    check("the turn really ended in edit mode", str(bpy.context.mode).startswith("EDIT_"))
    check("no push was attempted in edit mode", push_labels == pushes_before)
    receipt = session.last_receipt
    check("the receipt exists", isinstance(receipt, dict))
    if isinstance(receipt, dict):
        check("and it is not undoable", receipt["undoable"] is False)
        check("the title says so", receipt["title"] == "Not undoable")
        check("the reason is edit mode", receipt["reason"] == bc.undo.REFUSED_EDIT_MODE)
        check(
            "and the receipt says why in the terms the measurement used",
            any("edit mode" in line for line in receipt["lines"]),
        )
        check(
            "it does not claim Ctrl+Z reverts the turn",
            not any("Ctrl+Z reverts this turn" in line for line in receipt["lines"]),
        )
        check(
            "and the coverage sentence is still there",
            any("not files, network" in line for line in receipt["lines"]),
        )
    enter_object_mode()
    note(f"back in {bpy.context.mode}")


def case_6(session) -> None:
    """With Global Undo off, Send is refused (box 7)."""
    note("--- case 6: Global Undo off pauses auto-run")
    prefs = bpy.context.preferences.edit
    was = prefs.use_global_undo
    prefs.use_global_undo = False
    try:
        sent_before = len(worker.sent)
        settings.prompt_text = "this must not run"
        try:
            # An operator that reports an ERROR and cancels makes `bpy.ops` raise
            # in Python, which is how the refusal reaches a script - and it is the
            # same `self.report` the status bar shows to a human.
            result = repr(bpy.ops.blender_copilot.send())
            refused = "CANCELLED" in result
        except BaseException as exc:  # noqa: BLE001 - the refusal IS the result
            result = f"{type(exc).__name__}: {exc}"
            refused = True
        note(f"send with Global Undo off -> {result}")
        check("Send is refused", refused)
        check("and the refusal names the pause", "Auto-run is paused" in result)
        check("nothing was sent to the transport", len(worker.sent) == sent_before)
        check("and no turn opened", not session.streaming and session.phase == "idle")
        check(
            "the panel's own rule agrees",
            bc.undo_blender.pause_reason(bpy.context) == bc.undo.PAUSE_GLOBAL_UNDO,
        )
    finally:
        prefs.use_global_undo = was
        settings.prompt_text = ""
    check("the preference was put back", bpy.context.preferences.edit.use_global_undo is True)


def case_7() -> None:
    """CONTROL: with Global Undo off, a push is neither a detector nor a step.

    This is the case that fails ticket 12's addendum. It claimed a second,
    mechanical detector - "attempting the push fails, so the addon need not read
    the preference" - and the push does not fail: it reports `FINISHED` and
    records nothing. That is why `pause_reason` reads the preference.

    What "records nothing" looks like afterwards is *not* the same in both kinds
    of session, and both shapes are worth having in the log:

      * with no stack behind it, `ed.undo()` raises `poll() failed` and nothing
        moves (this probe's headless run, and ticket 15's fresh-session run);
      * with a stack behind it, `ed.undo()` returns `FINISHED` and jumps to a
        state *older* than the push (this probe's windowed run) - which is ticket
        15 run 1's "contaminated" reading, and it is a real behaviour rather than
        a bad measurement: the preference genuinely does not stop the operator
        walking a stack that already exists.

    Either way the turn is not recoverable, which is the whole of §4's case for
    pausing. The check below therefore asserts the part that is the same in both.
    """
    note("--- case 7: CONTROL - what a push does with Global Undo off")
    enter_object_mode()
    prefs = bpy.context.preferences.edit
    was = prefs.use_global_undo
    prefs.use_global_undo = False
    try:
        try:
            pushed = repr(bpy.ops.ed.undo_push(message="probe: with global undo off"))
        except BaseException as exc:  # noqa: BLE001 - the result is the finding
            pushed = f"RAISED {type(exc).__name__}: {exc}"
        at_push = object_names()
        undid = undo_once()
        after = object_names()
    finally:
        prefs.use_global_undo = was
    note(f"undo_push with Global Undo off -> {pushed}")
    note(f"undo with Global Undo off     -> {undid}")
    note(f"objects at the push: {at_push}")
    note(f"objects after that undo: {after}")
    check("the push REPORTS SUCCESS with Global Undo off", "FINISHED" in pushed)
    check(
        "so a push is not a detector, and the preference has to be read instead",
        "FINISHED" in pushed,
    )
    check(
        "and no usable step was recorded: Ctrl+Z does not give back the pushed state",
        undid.startswith("RAISED") or after != at_push,
    )


def case_8() -> None:
    """CONTROL: what case 5's refusal prevented."""
    note("--- case 8: CONTROL - a push taken in edit mode, then one undo")
    enter_object_mode()
    bpy.ops.ed.undo_push(message="probe: baseline before the hazard")
    bpy.ops.mesh.primitive_cube_add(size=1)
    probe = bpy.context.active_object
    probe.name = "Probe7"
    bpy.context.view_layer.update()
    before = object_names()
    note(f"created {probe.name}; objects {before}")
    bpy.ops.object.mode_set(mode="EDIT")
    note(f"mode is now {bpy.context.mode}")
    try:
        pushed = repr(bpy.ops.ed.undo_push(message="probe: edit-mode push"))
    except BaseException as exc:  # noqa: BLE001 - the result is the finding
        pushed = f"RAISED {type(exc).__name__}: {exc}"
    undid = undo_once()
    after = object_names()
    removed = sorted(set(before) - set(after))
    note(f"undo_push in edit mode -> {pushed}")
    note(f"the undo after it       -> {undid}")
    note(f"objects {len(before)} -> {len(after)}; removed={removed}; mode={bpy.context.mode}")
    check("the edit-mode push reports success too", "FINISHED" in pushed)
    check(
        "and the undo after it removes the object being edited",
        removed == ["Probe7"],
    )
    note("this is the measurement case 5's refusal exists for")
    enter_object_mode()


def case_10(session) -> None:
    """A turn that STARTS and RUNS in edit mode: allowed, unprotected, and measured.

    The owner's decision of 2026-09-26 was that a user may take this risk **knowingly**
    - they asked why Send was disabled while editing a cube, and chose the warning over
    the block. So the job of this case is to put a number on what they are accepting
    rather than an adjective:

      * the turn is accepted in edit mode at all (it was refused before this case
        existed - that refusal is what `case_6` pins for Global Undo off, and what this
        case proves is *not* the behaviour for edit mode);
      * no push is attempted, which is the destructive part kept out of reach;
      * the object being edited survives - the hazard `case_8` demonstrates needs a push
        to fire;
      * and Ctrl+Z afterwards does <what the log says>, which is the risk itself.

    A baseline is pushed first, as the addon's own first turn would, so "reaches past
    it" is detectable: if the undo takes the baseline's work with it, the log says so.
    """
    note("--- case 10: a turn that starts and runs in edit mode (owner's decision)")
    enter_object_mode()
    bpy.ops.object.select_all(action="SELECT")
    bpy.data.objects["Cube"].select_set(True)
    bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]

    # The fixture is built so the interesting question has an answer. "Did Ctrl+Z revert
    # the turn" and "did it reach past the turn to older work" produce the SAME state if
    # nothing sits between them, so a step of the user's own is pushed after the
    # baseline: scale 1.0 at the baseline, 2.0 in the user's own step, 3.0 set by the
    # turn. The undo landing on 3.0 -> 2.0 means it reverted exactly the turn; landing on
    # 1.0 means it ate the user's own step as well.
    baseline = scale_z()
    bpy.ops.ed.undo_push(message="probe: baseline")
    bpy.data.objects["Cube"].scale.z = 2.0
    bpy.context.view_layer.update()
    bpy.ops.ed.undo_push(message="probe: the user's own step")
    mine = scale_z()
    note(f"fixture: baseline scale {baseline}, the user's own step {mine}")

    # Enter edit mode the way the owner was: through the UI's own operator.
    bpy.ops.object.mode_set(mode="EDIT")
    check("the session really is in edit mode", str(bpy.context.mode).startswith("EDIT_"))
    stop = bc.undo_blender.blocking_reason(bpy.context)
    note(f"blocking_reason in edit mode -> {stop!r}")
    check("and Send is NOT blocked there any more", stop is None)

    pushes_before = list(push_labels)
    names_before = object_names()
    # A real turn, through the real operator, whose code changes the scene. The change
    # is a scale on the existing cube: the receipt's own docstring says the bounded
    # summary cannot see a transform, so this also exercises the `depsgraph_update_post`
    # flag rather than the diff.
    code = "cube = bpy.data.objects['Cube']\ncube.scale.z = 3.0\nprint('scaled in', C.mode)"
    if not turn("scale the cube while I am editing", [tool_round("c10", "Scale the cube", code), prose_round("Scaled.")]):
        enter_object_mode()
        return
    after_scale = scale_z()
    note(f"after the turn: mode={bpy.context.mode}, Cube.scale.z={after_scale}")
    check("the turn left the user in edit mode", str(bpy.context.mode).startswith("EDIT_"))
    check("the turn's change landed", after_scale == 3.0)
    check("no push was attempted in edit mode", push_labels == pushes_before)
    check("and the object being edited still exists", "Cube" in object_names())

    receipt = session.last_receipt
    if isinstance(receipt, dict):
        note(f"receipt: title={receipt['title']!r} undoable={receipt['undoable']} reason={receipt.get('reason')!r}")
        check("the receipt says the turn is not undoable", receipt["undoable"] is False)
        check(
            "and names edit mode as the reason",
            receipt.get("reason") == bc.undo.REFUSED_EDIT_MODE,
        )
        check(
            "the receipt does not promise Ctrl+Z reverts it",
            not any("Ctrl+Z reverts this turn" in line for line in receipt["lines"]),
        )

    # THE RISK, measured rather than described. One Ctrl+Z while still in edit mode,
    # then read the scale against the three known values.
    undid = undo_once()
    landed = scale_z()
    note(f"Ctrl+Z in edit mode -> {undid}; Cube.scale.z={landed}, objects={object_names()}")
    check("the undo did not delete the object being edited", "Cube" in object_names())
    if landed == mine:
        note(
            "RESULT: Ctrl+Z reverted exactly the turn's change (3.0 -> 2.0) and left the "
            "user's own earlier step alone"
        )
    elif landed == baseline:
        note(
            "RESULT: Ctrl+Z reached PAST the turn (3.0 -> 1.0) and undid the user's own "
            "earlier step as well - that is the risk, and the panel copy says so"
        )
    else:
        note(f"RESULT: Ctrl+Z landed somewhere else: scale {landed} (3.0 turn, 2.0 own step, 1.0 baseline)")

    # And once more from Object mode, which is where a user usually presses it.
    enter_object_mode()
    undid_object = undo_once()
    note(
        f"Ctrl+Z from object mode -> {undid_object}; Cube.scale.z={scale_z()}, "
        f"objects={object_names()}"
    )
    check("and that one did not delete anything either", "Cube" in object_names())
    note(
        f"the numbers above ARE the risk the owner accepted: {baseline} baseline, "
        f"{mine} the user's own step, 3.0 the turn"
    )


def case_9(session) -> None:
    """A last real turn, so a *pushed* receipt is what the screen shows.

    Worth having on its own: it shows the two controls above left the normal path
    working - the preference came back, the mode came back, and the next turn
    still pushes exactly one step.
    """
    note("--- case 9: a normal turn after the controls")
    enter_object_mode()
    stale = bpy.data.objects.get("Marker")
    if stale is not None:
        bpy.data.objects.remove(stale, do_unlink=True)
    pushes_before = list(push_labels)
    code = (
        "marker = D.objects.new('Marker', None)\n"
        "C.scene.collection.objects.link(marker)\n"
        "C.view_layer.update()\n"
        "print('linked', marker.name)"
    )
    rounds = [tool_round("c9", "Add Empty Marker", code), prose_round("Added.")]
    if not turn("add a marker empty", rounds):
        return
    check("the turn pushed exactly one step", len(push_labels) == len(pushes_before) + 1)
    receipt = session.last_receipt or {}
    note(f"receipt.changed = {receipt.get('changed')}")
    check("and the receipt is undoable again", receipt.get("undoable") is True)
    check(
        "with the change named",
        any(line.startswith("objects:") for line in receipt.get("changed", [])),
    )


def photograph_turn(session) -> None:
    """Clear the transcript, then run one real turn, so the receipt is on screen.

    The panel emits the whole transcript, the *region* scrolls, and the receipt is
drawn below the transcript because that is the turn it describes - so in a
nine-case conversation it is off the fold and a photograph of the panel is a
photograph of the transcript. Nothing about the receipt is faked here: this is the
same Send, the same push, and the same `undo.receipt`; only the conversation above
it is emptied first.
    """
    note("--- photograph: one turn on a cleared conversation")
    session.clear()
    code = (
        "cube = D.objects['Cube']\n"
        "cube.scale.z = 1.75\n"
        "marker = D.objects.new('Marker', None)\n"
        "C.scene.collection.objects.link(marker)\n"
        "C.view_layer.update()\n"
        "print('scaled and linked')"
    )
    rounds = [tool_round("p1", "Scale Cube and add Marker", code), prose_round("Done.")]
    if not turn("make the cube taller and add a marker", rounds):
        return
    receipt = session.last_receipt or {}
    note(f"photographed receipt: {receipt.get('title')} / {receipt.get('changed')}")
    check("the photographed turn is undoable", receipt.get("undoable") is True)


# ---------------------------------------------------------------------------
# The screenshot (GUI runs only)
# ---------------------------------------------------------------------------

def screenshot(path: Path) -> str:
    """Save the sidebar with the Copilot tab open, receipt and all.

    Called from a timer, never from the startup script: a GUI Blender does not
    paint what it has just been asked to show, and does not quit, until its event
    loop runs. Both were measured the hard way - the first version of this probe
    photographed an un-requested tab and then hung at `quit_blender` until
    `bounded_run`'s deadline killed it.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return "no window"
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        return "no 3D Viewport"
    region = next((r for r in area.regions if r.type == "UI"), None)
    if region is None:
        return "no sidebar region"
    try:
        region.active_panel_category = "Copilot"
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        note(f"could not select the Copilot tab: {exc}")
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.screen.screenshot_area(filepath=str(path))
        return f"{result} -> {path.name}"
    except Exception as exc:  # noqa: BLE001 - reported
        return f"failed: {type(exc).__name__}: {exc}"


def gui_tail() -> None:
    """Open the sidebar, let it paint, photograph it, then finish.

    A timer chain with one step per tick, because each step has to wait for the
    event loop to draw the result of the one before it: the sidebar is opened on
    one tick, the tab selected on the next (the assignment is rejected until the
    region has been laid out - measured in `loop_panel_probe.py`), the screenshot
    taken on the third, and the verdict written on the fourth.
    """
    state = {"step": 0}

    def step():
        if state["step"] == 0:
            window = getattr(bpy.context, "window", None)
            for area in getattr(window, "screen", None).areas if window else []:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 1
            return 0.15
        if state["step"] == 1:
            note(f"screenshot: {screenshot(SHOT)}")
            state["step"] = 2
            return 0.15
        finish()
        return None

    bpy.app.timers.register(step, first_interval=0.15)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def main() -> int:
    global bc, worker, settings, session, push_labels

    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None:
        # `--factory-startup` does not load user preferences, so the extension is
        # installed but not enabled. Enabling it in-script is what makes this run
        # both reproducible (a pristine session, Global Undo at its default) and
        # real (the extension's own `register`, its handlers, its operators).
        bpy.ops.preferences.addon_enable(module=EXT)
        addon = bpy.context.preferences.addons.get(EXT)
    check(
        "the extension registers with preferences",
        addon is not None and addon.preferences is not None,
    )
    if addon is None or addon.preferences is None:
        return 1
    settings = addon.preferences
    settings.layout_variant = "boxes"

    worker = ScriptedWorker()
    bc.transport.worker = worker
    session = bc.conversation.session

    # Record every push by wrapping the module's own `_push`: the real operator
    # still runs (the undo stack below is built by it), and the labels stop being
    # invisible. Nothing else can read a step's name back - `wm.bl_rna` exposes no
    # undo-stack property (ticket 12) - so this is the only way to show that the
    # turn's own label is what reaches `undo_push`.
    push_labels = []
    real_push = bc.undo_blender._push

    def recording_push(label_text: str):
        push_labels.append(label_text)
        return real_push(label_text)

    bc.undo_blender._push = recording_push

    # A scene this probe can make claims about: one cube, active, at a known size.
    # The user's own startup file is not trusted for that.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablock in (bpy.data.objects, bpy.data.meshes):
        for item in list(datablock):
            if item.users == 0:
                datablock.remove(item)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    enter_object_mode()
    bpy.context.view_layer.update()
    check("Global Undo is on, which every case below assumes", bool(bpy.context.preferences.edit.use_global_undo))
    note(f"scene prepared: {object_names()}, active={cube.name}, mode={bpy.context.mode}")
    note(f"blender {bpy.app.version_string}, background={bpy.app.background}")

    case_1(session)
    case_2(session)
    case_3(session)
    case_4(session)
    case_5(session)
    case_6(session)
    case_7()
    case_8()
    case_10(session)
    case_9(session)

    if not bpy.app.background:
        # A GUI Blender neither paints nor quits while this script runs, so the
        # rest of the run happens from a timer, and `finish` is called there.
        photograph_turn(session)
        gui_tail()
        return 0
    finish()
    return 0


def finish(code: int = 0) -> None:
    """Write the log, print the verdict, and quit a GUI Blender.

    One function for both paths so the token cannot be printed by one of them and
    not the other - the verdict is the thing every caller greps for, and a run
    that ends without it would look like a hang rather than a failure.
    """
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"UNDO | could not write {LOG}: {exc}", flush=True)
    for failure in FAILURES:
        print(f"UNDO | FAILED: {failure}", flush=True)
    print("SMOKE OK" if not FAILURES and code == 0 else "SMOKE FAILED", flush=True)
    if not bpy.app.background:
        # The probe quits Blender itself, so the bounded driver's deadline stays a
        # backstop rather than the mechanism.
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)
    elif FAILURES or code:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback

        traceback.print_exc()
        FAILURES.append("the probe itself raised")
        finish(1)
    # In background mode `main` has already called `finish`. In a GUI run `finish`
    # is called from the timer chain instead, because Blender has to enter its
    # event loop before the panel can paint the receipt or the process can quit.
