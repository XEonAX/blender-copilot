#!/usr/bin/env python3
"""Does the panel *move* while the model is silent, and what does the strip look like?

    python3 tools/bounded_run.py 150 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --python tools/working_probe.py

The subject is the working indicator, so the probe's whole design follows from one
fact: an indicator has to be caught **while nothing else changes**. A model that is
thinking sends no events at all, so the scripted worker below stays deliberately
silent for three seconds - no deltas, no tool calls, no completion - and the only
thing allowed to alter the sidebar in that window is the animation itself. Two
screenshots taken a second apart during it are therefore evidence about exactly one
thing: whether the pixels moved.

That is the difference between this and `tools/loop_panel_probe.py`, which also
runs with a scripted worker but never leaves it silent. What it cannot show: that a
*real* model makes the panel feel responsive - only `tools/live_turn_probe.py` makes
a real request - and whether the motion is pleasant to look at, which is a judgement
about the pictures this writes.

It also answers, without asserting, a question about the *alternative* design: can
the sidebar region be scrolled from Python? If it can, chronological order plus
auto-follow would be a real option; if it cannot, newest-first is the only way to
keep the live turn under the controls, since a Panel cannot scroll itself.

Writes `logs/working-strip-a.png` and `logs/working-strip-b.png`, prints a token and
quits Blender itself. Grep for `SMOKE OK`: Blender exits 0 even when a `--python`
script raises.
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
# so nothing is sent and no credential is read: `.env` is not loaded here and the
# key is a literal fake.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-working-probe")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "working.txt"
SHOT_A = ROOT / "logs" / "working-strip-a.png"
SHOT_B = ROOT / "logs" / "working-strip-b.png"

TICK = 0.1
# How long the scripted model says nothing. Long enough for two screenshots a
# second apart with room either side, short enough to stay inside the bound.
STALL_SECONDS = 3.0
TURN_DEADLINE = 30.0

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(line)
    print(f"WORKING | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


# ---------------------------------------------------------------------------
# The scripted transport: the worker protocol, and one long silence.
# ---------------------------------------------------------------------------

def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


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


class StallingWorker:
    """`transport.Worker`, except that one round says nothing for a while.

    The silence is the point, not a shortcut: an animation can only be shown to be
    running when there is no other reason for the panel to redraw. `polled` counts
    the ticks the drain asked for a reply and got none, which is what a model
    thinking looks like from here.

    A round is handed over when a **request** arrives, never on a tick of its own.
    The first version of this got that wrong and answered rounds nobody had asked
    for: the loop was still executing the tool call for round 1 when rounds 2 and 3
    were pushed at it, both were dropped as events for a turn that was not
    listening, and the turn then waited forever for a reply that had already been
    thrown away. Three seconds of confusion, and a lesson about fakes: the protocol
    is request-then-reply, so a fake that replies on a clock is not a fake of this
    transport at all.
    """

    def __init__(self, rounds: list[list[dict]], stall: float = 0.0) -> None:
        self.rounds = list(rounds)
        self.stall = stall
        self.started: float | None = None
        self.polled = 0
        self.silent = 0.0
        self.log: list[str] = []
        self.sent: list[list[dict]] = []
        self._queue: list[dict] = []

    @property
    def alive(self) -> bool:
        return True

    @property
    def busy(self) -> bool:
        # The queue matters as much as the rounds: during the stall the round has
        # been taken out of `rounds` and is waiting in the queue, and a `busy` that
        # went False there would let the drain timer unregister mid-turn.
        return bool(self._queue or self.rounds)

    def send(self, cfg, messages, tools=None) -> str | None:
        # Re-stamped on EVERY request, not just the first: the stall belongs to the
        # request it is set for, and a single stamp meant the second turn inherited a
        # silence that had already expired.
        self.started = time.monotonic()
        self.sent.append(messages)
        self._queue.extend(self.rounds.pop(0) if self.rounds else [])
        self.log.append(f"t={time.monotonic() - STARTED:.2f} request {len(self.sent)}")
        return None

    def tick(self) -> list[dict]:
        if self.started is None:
            return []
        if time.monotonic() - self.started < self.stall and self._queue:
            self.polled += 1
            self.silent = time.monotonic() - self.started
            return []
        events, self._queue = self._queue, []
        if events:
            self.log.append(
                f"t={time.monotonic() - STARTED:.2f} reply "
                + ",".join(str(event.get("ev")) for event in events)
            )
        return events

    def cancel(self) -> None:
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# What only a window can answer
# ---------------------------------------------------------------------------

def screenshot(path: Path) -> str:
    """Save the 3D Viewport with its sidebar open and the Copilot tab selected."""
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
        with bpy.context.temp_override(window=window, area=area, region=region):
            result = bpy.ops.screen.screenshot_area(filepath=str(path))
        return f"{result} -> {path.name}"
    except Exception as exc:  # noqa: BLE001 - reported
        return f"failed: {type(exc).__name__}: {exc}"


def scroll_report() -> str:
    """Can the sidebar region be scrolled from Python? Reported, never asserted.

    This is the honest answer to "why not keep chronological order and just follow
    the newest line": if the region's offset is writable, auto-follow is a real
    option for a later pass; if it is not, newest-first is the only way to keep the
    live turn under the controls. Either answer is a finding, which is why it is a
    printed report and not a check.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return "no window: not answerable"
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
    region = next((r for r in area.regions if r.type == "UI"), None) if area else None
    if region is None:
        return "no sidebar region: not answerable"

    view2d = getattr(region, "view2d", None)
    names = []
    try:
        names = sorted(bpy.types.View2D.bl_rna.properties.keys())
    except Exception as exc:  # noqa: BLE001 - reported
        names = [f"(unreadable: {type(exc).__name__})"]
    offsetable = [name for name in names if "offset" in name or name == "cur"]
    attempts = []
    if view2d is not None:
        for name in ("offset_y", "cur"):
            try:
                before = getattr(view2d, name)
                setattr(view2d, name, before)
                attempts.append(f"{name} readable+writable ({before})")
            except Exception as exc:  # noqa: BLE001 - reported
                attempts.append(f"{name} refused: {type(exc).__name__}: {exc}")
    return (
        f"region.view2d={'present' if view2d is not None else 'absent'}; "
        f"offset-ish properties={offsetable or 'none'}; writes: {'; '.join(attempts) or 'n/a'}"
    )


def rna_report() -> str:
    """The busy primitives this add-on depends on, from the live RNA.

    `hasattr` on an RNA type is an invalid test (AGENTS.md); the function table and
    its enums are the valid one. Kept in a committed probe so the dependency cannot
    rot silently under a Blender upgrade.
    """
    functions = bpy.types.UILayout.bl_rna.functions
    progress = functions.get("progress")
    if progress is None:
        return "UILayout.progress: ABSENT"
    param = progress.parameters.get("type")
    types = [item.identifier for item in param.enum_items] if param else []
    return f"UILayout.progress types: {types}"


def main() -> int:
    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        note("FAILED: no preferences - the extension is not registered as an add-on")
        return 1
    settings = addon.preferences
    if not bpy.context.preferences.edit.use_global_undo:
        note("FAILED: Global Undo is off, so Send is disabled by design")
        return 1
    if not settings.newest_first:
        # Not a failure: the indicator is what this probe is about, and it does not
        # care which end the newest turn is drawn at. Which order the pictures show
        # is recorded so the note is not mistaken for evidence about the layout.
        note(
            "note: 'newest turn first' is switched OFF in this session, so the "
            "pictures show chronological order"
        )

    note(rna_report())

    worker = StallingWorker(
        [
            tool_round("call_1", "Set a marker property", "print('scripted')"),
            # A reply long enough to wrap into a paragraph, because the paragraph is
            # the thing the picture is for: a one-line reply cannot show whether the
            # lines of a block sit at 19 px or 31 px, and the difference is the whole
            # subject of `tools/spacing_probe.py`.
            prose_round(
                "Two calls, material first. The mesh had no material slots yet, so the "
                "link call failed, and the traceback came back on the next round "
                "exactly as the loop promises. The detail behind the expander names "
                "the line that raised, and the change made before it is still on the "
                "scene rather than unwound."
            ),
            prose_round("Second turn: this one arrives after a silence."),
        ],
        stall=0.0,
    )
    bc.transport.worker = worker

    redraws: list[int] = []
    original_redraw = bc.stream.tag_view3d_redraw

    def counting_redraw() -> int:
        tagged = original_redraw()
        redraws.append(tagged)
        return tagged

    bc.stream.tag_view3d_redraw = counting_redraw

    session = bc.conversation.session
    session.clear()
    state = {"step": 0, "turn_started": 0.0, "a": None, "b": None, "stall_redraws": None}

    def send_turn(text: str, stall: float = 0.0) -> None:
        worker.stall = stall
        settings.prompt_text = text
        result = bpy.ops.blender_copilot.send()
        note(f"send({text!r}, stall={stall}) -> {result}")
        if "FINISHED" not in result:
            FAILURES.append(f"Send refused: {result}")
        state["turn_started"] = time.monotonic()

    def verdict() -> None:
        note(f"screenshots: {SHOT_A.name}, {SHOT_B.name}")
        note(f"scroll: {scroll_report()}")
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            note(f"could not write {LOG}: {exc}")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("SMOKE OK" if not FAILURES else "SMOKE FAILED", flush=True)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)

    def poll():
        if not session.streaming:
            step = state["step"]
            if step == 0:
                # The sidebar first, so that the turn that follows is photographed
                # in a panel the reader can actually see.
                if redraws:
                    redraws.clear()
                state["step"] = 1
                return TICK
            if step == 1:
                window = bpy.context.window
                for area in window.screen.areas:
                    if area.type != "VIEW_3D":
                        continue
                    area.spaces.active.show_region_ui = True
                bc.stream.tag_view3d_redraw()
                state["step"] = 5
                return TICK
            if step == 5:
                # The tab on its own tick, and after the sidebar exists: measured
                # 2026-09-26, `Region.active_panel_category` is read-only until the
                # region has been laid out, so assigning it in the tick that opens
                # the sidebar raises and the panel is photographed closed. The first
                # run of this probe did exactly that.
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
                state["step"] = 2
                return TICK
            if step == 2:
                state["step"] = 3
                send_turn("make the cube taller")
                return TICK
            if step == 3:
                note(
                    f"turn 1 finished after {time.monotonic() - state['turn_started']:.2f}s, "
                    f"rows={len(session.messages)}"
                )
                for line in worker.log:
                    note(f"worker {line}")
                worker.log.clear()
                check("the strip is gone once the turn is over", session.busy_note() == "")
                check("and its clock reads zero", session.elapsed() == 0.0)
                # The second turn is the one that goes silent, so the indicator is
                # photographed with nothing else that could change a pixel.
                redraws.clear()
                state["step"] = 4
                send_turn("now say something after a long think", stall=STALL_SECONDS)
                return TICK
            if step == 4:
                note(f"turn 2 finished after {time.monotonic() - state['turn_started']:.2f}s")
                if state["stall_redraws"] is None:
                    FAILURES.append("the stall was never sampled")
                else:
                    check(
                        "the panel repainted repeatedly while the model said nothing",
                        state["stall_redraws"] >= 5,
                    )
                    note(f"repaints during the silent stall: {state['stall_redraws']}")
                check("the strip is gone again", session.busy_note() == "")
                verdict()
                return None
            return None

        # A turn is in flight. The second one is the stall under test.
        elapsed = time.monotonic() - state["turn_started"]
        # A trace of the first ticks, because "the turn never advanced" is
        # indistinguishable from "the timer never ran" in a verdict, and this probe
        # stalled a turn exactly once by not knowing which of the two it was.
        state["traced"] = state.get("traced", 0) + 1
        if state["traced"] <= 8 or state["traced"] % 40 == 0:
            note(
                f"tick {state['traced']}: phase={session.phase} status={session.status!r} "
                f"pending={len(session.pending)} rounds_left={len(worker.rounds)} "
                f"requests={len(worker.sent)} drain={bpy.app.timers.is_registered(bc.stream._tick)} "
                f"rows={len(session.messages)}"
            )
        if state["step"] == 4 and state["a"] is None and elapsed > 0.8:
            state["a"] = (conversation_ring(), session.busy_note(), round(session.elapsed(), 1))
            note(f"strip at t={elapsed:.1f}s: {state['a'][1]} {state['a'][2]}s")
            note(f"screenshot A: {screenshot(SHOT_A)}")
            state["stall_redraws"] = len(redraws)
        elif state["step"] == 4 and state["a"] is not None and state["b"] is None and elapsed > 1.8:
            state["b"] = (conversation_ring(), session.busy_note(), round(session.elapsed(), 1))
            note(f"strip at t={elapsed:.1f}s: {state['b'][1]} {state['b'][2]}s")
            note(f"screenshot B: {screenshot(SHOT_B)}")
            check(
                "the arc had moved on between the two pictures",
                state["a"][0] != state["b"][0],
            )
            check(
                "and the clock had advanced",
                state["b"][2] > state["a"][2],
            )
            # The words and the motion are two different claims, and only the
            # motion is supposed to change while the model is silent: the phrase is
            # read from the session's own state, so a hard-coded copy here would
            # test this file instead of the panel - and it did, the first time the
            # phrase was shortened.
            check(
                "while the words stayed put, because nothing had changed",
                state["a"][1] == state["b"][1] != "",
            )
            note(f"the phrase during the silence: {state['a'][1]!r}")
            try:
                moved = SHOT_A.read_bytes() != SHOT_B.read_bytes()
            except Exception as exc:  # noqa: BLE001 - reported
                note(f"could not compare screenshots: {exc}")
                moved = False
            check(
                "the two pictures differ, with nothing else on screen able to change",
                moved,
            )
        if elapsed > TURN_DEADLINE:
            note(f"last status: {session.status!r}, phase={session.phase}")
            for message in session.messages:
                note(f"row {message.kind}: {(message.text or message.purpose or '')[:120]!r}")
            for line in worker.log:
                note(f"worker {line}")
            FAILURES.append("the turn did not finish inside the deadline")
            verdict()
            return None
        return TICK

    def conversation_ring() -> float:
        return round(bc.conversation.ring_sweep(), 3)

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
