#!/usr/bin/env python3
"""One real tool call, driven through the panel in a real GUI session.

    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/loop_panel_probe.py

**The wire is faked; nothing above it is.** `tools/loop_wire_probe.py` verifies
the transport half - a real `_worker.py` child reassembling fragmented SSE deltas
from a scripted localhost provider - with no Blender. This verifies the other
half, and only a GUI session can: `bpy.app.timers` never pump under `blender -b`,
so the per-tick loop, the live sandbox with the real `bpy` prelude, the transcript
rows and the repaints all need a window.

What it proves, in the ticket's own words:

  * a request to change the cube changes the cube, from code that arrived as a
    tool call rather than from any line in this file (the code is a string in the
    scripted reply, handed to the same sandbox the model's code goes to);
  * the panel shows a tool row naming the call, with its output behind the
    expander, and a code identity row beside it;
  * a failing call returns the full traceback, the model gets it on the following
    round, and the change made before the raise is reported rather than undone;
  * the loop runs at most one call per timer tick, and the timer repaints.

What it does **not** prove: that the model writes that code, or that DeepSeek
accepts this request. No live request is made here or anywhere in this pass -
the ticket that spends money is the one reserved for it, and the scripted
provider in the wire probe stands in for the model's *decision*.

It writes `logs/loop-panel.txt`, tries to save `logs/loop-panel.png` (a screenshot
is evidence, so it is taken when it can be), prints a token, and quits Blender
itself so the bounded driver's deadline is a backstop and not the mechanism.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import bpy

# Dummy values so `transport.config()` is satisfied: the worker is replaced below,
# so nothing is ever sent and no real credential is read. `.env` is not loaded and
# the key here is a literal fake.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "loop-panel.txt"
SHOT = ROOT / "logs" / "loop-panel.png"
TICK = 0.1
TURN_DEADLINE = 20.0

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()

# The shipped base prompt, read off the add-on in `main()`. The probe must not
# carry its own copy: the check is that the request really carries the real one at
# index 0, which is what holds the provider's cache prefix across turns.
EXPECT: dict[str, str] = {}

# The code the "model" asks for. It arrives as `function.arguments` inside a
# scripted `tool_call`, which is the whole point: the probe never runs it itself.
CODE_TALL = """obj = C.active_object
if obj is None:
    raise RuntimeError("nothing is active")
obj.scale.z = obj.scale.z * 1.5
C.view_layer.update()
print(f"{obj.name}: z scale {obj.scale.z:.2f}, height {obj.dimensions.z:.2f}")"""

CODE_PARTIAL_THEN_FAIL = """obj = C.active_object
obj.scale.x = obj.scale.x * 2
C.view_layer.update()
raise RuntimeError("native call blew up after the change")"""


def note(line: str) -> None:
    NOTES.append(line)
    print(f"LOOP | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


# ---------------------------------------------------------------------------
# The scripted transport. Everything else in this run is the shipped code.
#
# The events below are the **worker protocol**, not raw SSE: this fake stands in
# for `transport.Worker`, so the layer beneath it - `_worker.py` turning HTTP SSE
# into these deltas - is out of frame here and verified by
# `tools/loop_wire_probe.py` instead. Getting that boundary wrong is how the first
# version of this probe stalled: SSE-shaped dicts are dropped by `apply_event`
# without a word, and the turn sat at "waiting…" for its whole deadline.
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


def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


class ScriptedWorker:
    """Stands in for `transport.Worker`: same methods, no pipe and no HTTP.

    Events are queued and handed over on the *next* `tick()`, which is what the
    real thing does too - the reader threads cannot deliver mid-`send`, so the
    first tick after a request is always the one that carries its reply.
    """

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.sent: list[dict] = []
        self.messages_at_send: list[list[dict]] = []
        self._queue: list[dict] = []
        self.cancels = 0

    @property
    def busy(self) -> bool:
        return bool(self._queue or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.messages_at_send.append(messages)
        self.sent.append({"messages": messages, "tools": tools})
        self._queue.extend(self.rounds.pop(0) if self.rounds else [])
        return None

    def tick(self) -> list[dict]:
        events, self._queue = self._queue, []
        return events

    def cancel(self) -> None:
        self.cancels += 1
        self._queue = []
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def unanswered(history: list[dict]) -> list:
    """Ids a `tool_calls` message asked about that no `tool` message answered.

    The provider's own rule (ticket 16, HTTP 400), which is why the flusher is
    load-bearing rather than tidy.
    """
    asked = [
        entry.get("id")
        for message in history
        if message.get("role") == "assistant"
        for entry in (message.get("tool_calls") or [])
    ]
    answered = [
        message.get("tool_call_id") for message in history if message.get("role") == "tool"
    ]
    return [call_id for call_id in asked if call_id not in answered]


def rows(session, kind):
    return [message for message in session.messages if message.kind == kind]


def scenario_1(session, worker, cube) -> None:
    """A change asked for in the prompt actually happens."""
    # The cube was created at size=2, so dimensions are 2/2/2 before this turn and
    # the assertion below is the whole of the claim: the sandbox moved it.
    note(
        f"cube after turn 1: dimensions.z={cube.dimensions.z:.3f} "
        f"scale.z={cube.scale.z:.3f}"
    )
    check("the request declared the tool", worker.sent[0]["tools"][0]["function"]["name"] == "run_blender_python")
    check("and it declared that tool to the live panel path", len(worker.sent) >= 2)
    check(
        "the cube changed, and no line of this probe ran the code",
        abs(cube.dimensions.z - 3.0) < 0.01,
    )
    codes = rows(session, "code")
    tools = rows(session, "tool")
    check("a code identity row was drawn", len(codes) >= 1 and codes[0].purpose == "Scale Cube 1.5x on Z")
    check("it carries the model's code for `Show code`", "obj.scale.z" in codes[0].detail)
    check("a tool row names what ran", bool(tools) and tools[0].purpose == "Scale Cube 1.5x on Z")
    check("its status is ok", tools[0].status == "ok")
    check("its output is behind the expander, not in the row", bool(tools[0].detail))
    check(
        "and the output is the envelope the model received",
        '"ok": true' in tools[0].detail and "z scale 1.50, height 3.00" in tools[0].detail,
    )

    second = worker.sent[1]["messages"]
    assistant = [message for message in second if message.get("role") == "assistant"]
    results = [message for message in second if message.get("role") == "tool"]
    check("the following round carries the assistant's tool_calls", bool(assistant[-1].get("tool_calls")))
    check("and a tool result answering its id", len(results) == 1)
    check("with the sandbox's envelope", '"ok": true' in results[0]["content"])
    check("the base prompt is still index 0", second[0]["content"] == EXPECT["base"])
    check(
        "and it names the tool it declares, so the model can use it",
        "run_blender_python" in EXPECT["base"],
    )
    check(
        "the live scene summary is still the last message",
        second[-1]["role"] == "system"
        # It is the `get_scene_info` summary scope, serialised (one definition for
        # both consumers), so it is JSON with a `captured` stamp rather than the
        # hand-written sentence it used to be.
        and json.loads(second[-1]["content"]).get("captured") == "turn_start",
    )
    check("the turn ended", not session.streaming and session.phase == "idle")
    check("history is sendable: nothing is unanswered", unanswered(session.history) == [])


def scenario_2(session, worker, cube) -> None:
    """A failing call: full traceback, fed back, and no unwinding."""
    before = cube.scale.x
    tools = rows(session, "tool")
    failure = tools[-1]
    check("the failing call has its own row", failure.purpose == "Try a bigger scale")
    check("marked as an error", failure.status == "error")
    check("with the full traceback behind the expander", "Traceback" in failure.detail)
    check(
        "naming what actually went wrong",
        "RuntimeError: native call blew up after the change" in failure.detail,
    )
    check("the half-applied change is still there, not unwound", abs(cube.scale.x - before) < 1e-6 and before == 2.0)
    last = worker.sent[-1]["messages"]
    results = [message for message in last if message.get("role") == "tool"]
    check(
        "the model received that traceback on the following round",
        "RuntimeError: native call blew up after the change" in results[-1]["content"],
    )
    check("the turn ended again", not session.streaming and session.phase == "idle")


def screenshot(path: Path) -> str:
    """Save the 3D Viewport with its sidebar open. Never fatal.

    A screenshot is evidence like any other, and one is only worth taking if it
    shows the thing under test: the sidebar is opened and the Copilot tab selected
    first, because a picture of a closed sidebar proves nothing about the panel.

    MEASURED 2026-09-26, 5.2.2: `Region.active_panel_category` is writable only
    once the sidebar is actually laid out - assigning it in the same timer tick
    that opens the sidebar raises "attribute is read-only" - and `WM_OT_splash`
    is an *invalid operator call* from a script context, so the startup splash
    cannot be dismissed from Python. The splash does not cover the sidebar, so the
    panel is still photographable; the tab is why this is called on its own tick.
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


def main() -> int:
    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        note("FAILED: no preferences - the extension is not registered as an add-on")
        return 1
    if not bpy.context.preferences.edit.use_global_undo:
        note("FAILED: Global Undo is off, so Send is disabled by design")
        return 1

    # A scene this probe can make claims about: exactly one cube, selected and
    # active, at a known size. The user's own startup file is not trusted for that.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    for candidate in list(bpy.data.objects):
        if candidate is not cube:
            bpy.data.objects.remove(candidate, do_unlink=True)
    bpy.context.view_layer.update()
    note(f"scene prepared: {cube.name}, active={bpy.context.view_layer.objects.active.name}")

    worker = ScriptedWorker(
        [
            tool_round("call_1", "Scale Cube 1.5x on Z", CODE_TALL, "The cube is active, so I'll scale it."),
            prose_round("Done: the cube is 1.5x taller on Z."),
            tool_round("call_2", "Try a bigger scale", CODE_PARTIAL_THEN_FAIL, "Trying a bigger change."),
            prose_round("That failed, and what it changed is still there."),
        ]
    )
    bc.transport.worker = worker

    # Repaints, counted through the one function the drain calls - the same trick
    # `panel_round_trip.py` uses, and the only way to tell a live timer from a
    # dormant one.
    redraws: list[int] = []
    original_redraw = bc.stream.tag_view3d_redraw

    def counting_redraw() -> int:
        tagged = original_redraw()
        redraws.append(tagged)
        return tagged

    bc.stream.tag_view3d_redraw = counting_redraw

    session = bc.conversation.session
    session.clear()
    EXPECT["base"] = bc.prompt.BASE_PROMPT
    preferences = addon.preferences
    state = {"step": 0, "turn_started": 0.0}

    def send_turn(prompt: str) -> None:
        preferences.prompt_text = prompt
        result = bpy.ops.blender_copilot.send()
        note(f"send({prompt!r}) -> {result}")
        if "FINISHED" not in result:
            FAILURES.append(f"Send refused: {result}")
        state["turn_started"] = time.monotonic()

    def main_start() -> None:
        # `show_splash = False` applies to windows created from here on; the one
        # already on screen cannot be dismissed from a script (WM_OT_splash is an
        # invalid operator call there), which is fine - the splash sits in the
        # middle of the window and does not cover the sidebar.
        try:
            bpy.context.preferences.view.show_splash = False
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            note(f"could not set show_splash: {exc}")

    def verdict() -> None:
        if not FAILURES:
            note(f"session status: {session.status!r}, transcript rows: {len(session.messages)}")
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            note(f"could not write {LOG}: {exc}")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("SMOKE OK" if not FAILURES else "SMOKE FAILED", flush=True)
        # Blender quits itself, so the bounded driver's deadline stays a backstop
        # rather than the mechanism.
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)

    def poll():
        """The harness's own timer: it watches, the add-on's drain does the work.

        Steps rather than a state machine, because each one has to wait for the
        real turn to finish and there is nothing else to do meanwhile.
        """
        if session.streaming:
            # A short trace of the first ticks, because "the turn never advanced"
            # is indistinguishable from "the timer never ran" in the verdict.
            state["traces"] = state.get("traces", 0) + 1
            if state["traces"] <= 6:
                note(
                    f"tick {state['traces']}: phase={session.phase} "
                    f"drain_registered={bpy.app.timers.is_registered(bc.stream._tick)} "
                    f"queued={len(worker._queue)} rows={len(session.messages)}"
                )
            if time.monotonic() - state["turn_started"] > TURN_DEADLINE:
                note(f"last status: {session.status!r}")
                FAILURES.append("the turn did not finish inside the deadline")
                verdict()
                return None
            return TICK

        step = state["step"]
        if step == 0:
            note(f"turn 1 finished after {time.monotonic() - STARTED:.2f}s")
            scenario_1(session, worker, cube)
            state["step"] = 1
            send_turn("now make it much taller")
            return TICK

        if step == 1:
            note(f"turn 2 finished after {time.monotonic() - STARTED:.2f}s")
            scenario_2(session, worker, cube)

            check("the timer repainted while the turn was in flight", bool(redraws))
            check("and the repaint found a region to tag", any(redraws))
            purposes = [
                message.purpose for message in rows(session, "tool") if message.purpose
            ]
            note(f"tool rows, in order: {purposes}")
            check(
                "every call got exactly one row, in order",
                purposes == ["Scale Cube 1.5x on Z", "Try a bigger scale"],
            )

            # Show both expander states in one picture: the successful call's
            # output collapsed, the failed call's traceback open.
            for message in rows(session, "tool"):
                if message.purpose == "Try a bigger scale":
                    message.expanded = True
            # Open the sidebar on this tick...
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 2
            return TICK

        # ...and select the tab on the next one, because the assignment is only
        # accepted once the region has been laid out.
        if step == 2:
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type != "UI":
                        continue
                    try:
                        region.active_panel_category = "Copilot"
                    except Exception as exc:  # noqa: BLE001 - reported
                        note(f"could not select the Copilot tab: {exc}")
            bc.stream.tag_view3d_redraw()
            state["step"] = 3
            return TICK

        if step == 3:
            note(f"screenshot: {screenshot(SHOT)}")
            state["step"] = 4
            return TICK

        if step == 4:
            verdict()
            return None

        return None

    send_turn("make the cube taller")
    check("the turn opened", session.streaming)
    main_start()
    bpy.app.timers.register(poll, first_interval=TICK)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("SMOKE FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
