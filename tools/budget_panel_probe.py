#!/usr/bin/env python3
"""What the panel says while code is running, and after it was stopped.

    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/budget_panel_probe.py

**The budget is real, the panel is real, the wire is faked.** Everything that
decides the outcome is shipped code - `budget.py`'s alarm, `execution.run_python`,
the loop in `conversation.py`, `undo_blender` and the panel's own draw bodies -
and the call is stopped by the alarm rather than by anything this file does. The
only fake is the transport: a scripted worker stands in for `transport.Worker`, so
no request leaves the machine and no credential is read.

Why this needs a window: the panel cannot be photographed under `blender -b`, and
the whole point of the ticket is what the user *reads*. Two things only a screen
can settle, and both are photographs:

  * the running-call note while a call is queued and about to run - the last moment
    at which it can be drawn, because the panel is frozen for the call's whole
    duration and Blender does not repaint until it returns;
  * the stopped turn: the sentence naming what actually happened, the transcript
    row, and the undo receipt, all in place afterwards.

The probe drives the loop itself (`stream._tick()`, the function `bpy.app.timers`
calls) with the add-on's own timer stopped, so it can photograph the exact tick
between "the call is queued" and "the call is running". That is the only way to
see that state at all - the addon's timer runs the call on the next tick, 50 ms
later, and one of the things measured here is that this window is as narrow on
screen as the design says it is.

It writes `logs/budget-panel.txt` and `logs/budget-panel.png` /
`logs/budget-stopped.png`, prints a token, and quits Blender itself so the bounded
driver's deadline is a backstop and not the mechanism.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import bpy

# A literal fake: the worker is replaced below, so nothing is sent and `.env` is
# never read.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-budget-panel")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "budget-panel.txt"
SHOT_RUNNING = ROOT / "logs" / "budget-panel.png"
SHOT_STOPPED = ROOT / "logs" / "budget-stopped.png"

# Seconds rather than the shipped 15: the shape of the run is the same and the
# probe stays inside a bound. `poll` is the alarm's own repeat interval.
CALL_SECONDS = 2.0
TURN_SECONDS = 30.0
POLL_SECONDS = 0.5
TICK = 0.1
TURN_DEADLINE = 25.0

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()

bc = None
session = None
worker = None
redraws: list[int] = []

# The code the "model" asks for: change the cube, then never return. It is handed
# to the real sandbox as `function.arguments`, so no line of this probe runs it.
CODE = """obj = C.active_object
obj.scale.z = 2.0
C.view_layer.update()
print(f"{obj.name}: z scale is now {obj.scale.z:.2f}")
while True:
    pass"""


def note(line: str) -> None:
    NOTES.append(line)
    print(f"PANEL | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


class ScriptedWorker:
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


def last_error_text() -> str:
    for message in reversed(session.messages):
        if message.kind == "error":
            return message.text
    return ""


def screenshot(path: Path) -> str:
    """Save the 3D Viewport with its sidebar open. Never fatal.

    MEASURED 2026-09-26, 5.2.2: `Region.active_panel_category` is writable only
    once the sidebar has actually been laid out, so the tab is selected on its own
    tick; `WM_OT_splash` is an invalid operator call from a script context, so the
    startup splash cannot be dismissed from Python - it does not cover the sidebar,
    so the panel is still photographable.
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
    tab = "?"
    try:
        region.active_panel_category = "Copilot"
        tab = str(getattr(region, "active_panel_category", "?"))
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        tab = f"could not select ({type(exc).__name__}: {exc})"
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.screen.screenshot_area(filepath=str(path))
        return f"{result} -> {path.name}, sidebar tab {tab!r}"
    except Exception as exc:  # noqa: BLE001 - reported
        return f"failed: {type(exc).__name__}: {exc}"


def verdict() -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        note(f"could not write {LOG}: {exc}")
    for failure in FAILURES:
        note(f"FAILED: {failure}")
    print("BUDGET PANEL OK" if not FAILURES else "BUDGET PANEL FAILED", flush=True)
    # Blender quits itself, so the bounded driver's deadline stays a backstop
    # rather than the mechanism.
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        sys.exit(0 if not FAILURES else 1)


def main() -> int:
    global bc, session, worker

    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None:
        bpy.ops.preferences.addon_enable(module=EXT)
        addon = bpy.context.preferences.addons.get(EXT)
    check("the extension registers with preferences", addon is not None and addon.preferences is not None)
    if addon is None or addon.preferences is None:
        return 1
    if not bpy.context.preferences.edit.use_global_undo:
        note("FAILED: Global Undo is off, so Send is disabled by design")
        return 1
    settings = addon.preferences
    settings.layout_variant = "boxes"

    worker = ScriptedWorker()
    bc.transport.worker = worker
    session = bc.conversation.session

    # A scene this probe can make claims about: one cube, at a known scale.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablock in (bpy.data.objects, bpy.data.meshes):
        for item in list(datablock):
            if item.users == 0:
                datablock.remove(item)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    cube = bpy.context.active_object
    cube.scale.z = 1.0
    bpy.context.view_layer.update()

    # The shipped budget object, shortened. The loop opens the turn on this same
    # object (`session.limits is budget.LIMITS`), which is what the smoke checks.
    limits = bc.budget.LIMITS
    limits.call_seconds = CALL_SECONDS
    limits.turn_seconds = TURN_SECONDS
    limits.poll = POLL_SECONDS
    check("the session's budget is the object this probe shortened", session.limits is limits)

    original_redraw = bc.stream.tag_view3d_redraw

    def counting_redraw() -> int:
        tagged = original_redraw()
        redraws.append(tagged)
        return tagged

    bc.stream.tag_view3d_redraw = counting_redraw

    session.clear()
    state = {"step": 0, "queued_at": 0.0, "ran_at": 0.0}

    def send_turn() -> None:
        # The scripted reply *is* the model's decision: one call, whose code came
        # from this file's own string and goes to the real sandbox.
        worker.rounds = [
            tool_round(
                "p1",
                "Scale Cube 2x on Z, then spin",
                CODE,
                "The cube is active, so I'll scale it and then loop.",
            )
        ]
        settings.prompt_text = "spin forever"
        result = bpy.ops.blender_copilot.send()
        # The add-on's own timer is stopped after every send: this probe drives
        # `stream._tick()` itself so it can photograph the tick before the call
        # runs. Leaving the real timer registered would race it.
        bc.stream.stop()
        bc.stream.tag_view3d_redraw()
        check("Send started a turn", "FINISHED" in result and session.streaming)

    def poll():
        step = state["step"]

        # 1: one tick, which drains the scripted reply and queues the call's rows.
        # The call has NOT run: `_tick` does one thing per call, and this is the
        # only moment at which the panel can show a note about a call that is
        # about to block the main thread for its whole budget.
        if step == 0:
            bc.stream._tick()
            check("the rows are queued but nothing has run", session.running_tool is not None)
            check("the status says code is running", session.status == "running code")
            check("the queue holds the call, unrun", len(session.pending) == 1)
            check("and the cube is untouched so far", round(cube.scale.z, 3) == 1.0)
            check(
                "the panel's copy is the shipped pair, not this run's short numbers",
                f"{bc.budget.CALL_SECONDS:.0f}s per call" in bc.budget.RUNNING_LINES[0]
                and f"{bc.budget.TURN_SECONDS:.0f}s per turn" in bc.budget.RUNNING_LINES[0],
            )
            note(
                "the screenshots therefore read 15s/60s while this run was shortened "
                f"to {CALL_SECONDS}s/{TURN_SECONDS:.0f}s - the copy is a shipped "
                "constant and the shortening is the probe's"
            )
            # Open the sidebar on this tick, select the tab on the next: the
            # assignment is only accepted once the region has been laid out.
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 1
            return TICK

        if step == 1:
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "UI":
                        try:
                            region.active_panel_category = "Copilot"
                        except Exception as exc:  # noqa: BLE001 - reported
                            note(f"could not select the Copilot tab: {exc}")
            bc.stream.tag_view3d_redraw()
            state["step"] = 2
            return TICK

        # 2: photograph the running state, then run the call. The call blocks the
        # main thread for its whole budget - 2 s here - and the screenshot has to
        # be taken before it, because there is no repaint while it runs.
        if step == 2:
            note(f"screenshot (running): {screenshot(SHOT_RUNNING)}")
            state["queued_at"] = time.monotonic()
            bc.stream._tick()  # this is where the code runs, and is stopped
            state["ran_at"] = time.monotonic()
            state["step"] = 3
            return TICK

        # 3: the call is over. Everything about the stop is now observable.
        if step == 3:
            elapsed = state["ran_at"] - state["queued_at"]
            note(f"the call ran for {elapsed:.2f}s against a {CALL_SECONDS}s budget")
            check("the turn is over", not session.streaming and session.phase == "idle")
            check(
                "the loop ended inside a tick of its budget",
                CALL_SECONDS <= elapsed < CALL_SECONDS + 1.0,
            )
            verdict_of_call = limits.verdict()
            note(f"verdict: {verdict_of_call}")
            check("the interrupt took", verdict_of_call.get("interrupted") is True)
            check("and it was the call's budget, not the turn's", verdict_of_call.get("kind") == "call")
            check(
                "the panel names what happened, with the seconds it ran",
                "ran past its budget" in last_error_text(),
            )
            check(
                "the code's own print is behind the tool row's expander",
                any("z scale is now 2.00" in (m.detail or "") for m in session.messages),
            )
            check("the change it made before the loop is still applied", round(cube.scale.z, 3) == 2.0)
            receipt = session.last_receipt
            note(f"receipt: {receipt}")
            check("the stopped turn left a receipt", bool(receipt))
            check("and the receipt says the step is undoable", bool(receipt and receipt["undoable"]))
            check(
                "the panel keeps drawing the receipt under the transcript",
                bool(receipt and receipt["lines"]),
            )
            check("the timer repainted during the turn", any(redraws))
            state["step"] = 4
            return TICK

        if step == 4:
            note(f"screenshot (stopped): {screenshot(SHOT_STOPPED)}")
            state["step"] = 5
            return TICK

        # 5: the only revert there is. A GUI is where this claim belongs: the
        # interrupted turn has to be one Ctrl+Z of the *user's* Blender.
        if step == 5:
            undone = bpy.ops.ed.undo()
            note(f"one undo of the interrupted turn -> {undone}")
            survivor = bpy.context.active_object
            check(
                "one Ctrl+Z reverts the interrupted turn",
                survivor is not None and round(survivor.scale.z, 3) == 1.0,
            )
            # The add-on's timer must not be left registered by a probe, and the
            # alarm must not be left armed: either would change what the next
            # command in this session measures.
            bc.stream.stop()
            first, interval = signal.getitimer(signal.ITIMER_REAL)
            check("no alarm is left armed", first == 0.0 and interval == 0.0)
            check("and the add-on's timer is not registered by this probe", not bpy.app.timers.is_registered(bc.stream._tick))
            state["step"] = 6
            verdict()
            return None

        return None

    try:
        bpy.context.preferences.view.show_splash = False
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        note(f"could not set show_splash: {exc}")

    note(f"blender {bpy.app.version_string}, background={bpy.app.background}")
    note(
        f"budget for this run: {CALL_SECONDS}s per call, {TURN_SECONDS}s per turn, "
        f"alarm every {POLL_SECONDS}s"
    )
    send_turn()
    bpy.app.timers.register(poll, first_interval=TICK)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("BUDGET PANEL FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
