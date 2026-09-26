#!/usr/bin/env python3
"""A conversation that outgrows its budget, driven through the real panel.

    python3 tools/bounded_run.py 150 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/trim_panel_probe.py

**Only the model is faked.** The record, the budget (read from the add-on's own
preference, at its shipped minimum of 8 KiB), the projection, the request, the
transcript rows and the repaints are all the shipped code, in a real GUI session
where `bpy.app.timers` actually pump.

What it proves, in the ticket's own words:

  * a conversation long enough to exceed the budget is still answered: the turn
    completes, the request that went out fits the budget, and the history stays
    sendable (nothing unanswered);
  * the live scene summary is still the last message and the base prompt is still
    index 0, with the head marker between them - so the provider's cache prefix
    survives a trim;
  * the stored transcript is **unchanged** by trimming: every seeded message is
    still there, byte for byte, after the turn;
  * the panel shows that trimming is in effect (the header chip) and says which
    turns went (the note, collapsed and expanded) - photographed both ways,
    because a screenshot is evidence and the note is a *visual* claim.

What it does **not** prove: that DeepSeek accepts this request (no live request is
made; the ticket that spends money is the one reserved for it), that the glyphs
render on every platform, or anything about a user's own preference value - which
this probe sets and then puts back.

It writes `logs/context-trim.txt`, tries to save `logs/context-trim.png` and
`logs/context-trim-expanded.png`, prints a token, and quits Blender itself so the
bounded driver's deadline stays a backstop rather than the mechanism.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import bpy

# Dummy values so `transport.config()` is satisfied. The worker is replaced below,
# so nothing is ever sent and no real credential is read - `.env` is not loaded and
# the key here is a literal fake. The history directory is diverted so the probe
# cannot write conversations into the user's own config.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/bc-t06-history")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "context-trim.txt"
SHOT_FULL = ROOT / "logs" / "context-trim-full.png"
SHOT = ROOT / "logs" / "context-trim.png"
SHOT_OPEN = ROOT / "logs" / "context-trim-expanded.png"
TICK = 0.1
TURN_DEADLINE = 25.0
# The shipped minimum of the addon's own lever, so the budget this probe trims
# against is the real one rather than a fixture number.
HISTORY_KIB = 8

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(line)
    print(f"TRIM | {line}", flush=True)


def check(label: str, condition: bool, extra="") -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}" + ("" if extra == "" else f"  [{extra!r}]"))
    return condition


# ---------------------------------------------------------------------------
# The scripted transport: the worker protocol, no pipe and no HTTP.
# ---------------------------------------------------------------------------

def tool_round(call_id: str, purpose: str, code: str, prose: str) -> list[dict]:
    return [
        {"ev": "delta", "text": prose},
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
        },
    ]


def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


class ScriptedWorker:
    """Stands in for `transport.Worker`: same methods, no pipe and no HTTP."""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.sent: list[list[dict]] = []
        self._queue: list[dict] = []

    @property
    def busy(self) -> bool:
        return bool(self._queue or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.sent.append(messages)
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
# A record long enough to outgrow the budget
# ---------------------------------------------------------------------------

def seeded_history(turns: int = 6) -> list[dict]:
    """Six turns of a plausible session: prose, a call, and a fat tool result.

    Fat on purpose. The point of the run is the projection, and a conversation
    that fits proves nothing about it.
    """
    history: list[dict] = []
    for index in range(turns):
        call_id = f"seed{index}"
        history.append(
            {
                "role": "user",
                "content": (
                    f"Turn {index}: please look at the scene and tell me about "
                    "what is selected, then describe it for me in detail " * 2
                ),
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": (
                    f"Looking at the scene for turn {index}. I will read it back "
                    "before saying anything about it, because the summary I am "
                    "given can be stale by the time I act on it. " * 2
                ),
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "get_scene_info",
                            "arguments": json.dumps({"scope": "summary", "purpose": f"read the scene {index}"}),
                        },
                    }
                ],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(
                    {"ok": True, "objects": ["y" * 4_000], "note": "a fat result"},
                    ensure_ascii=False,
                ),
            }
        )
        history.append(
            {"role": "assistant", "content": f"Turn {index} is done, and here is what I saw."}
        )
    return history


CODE_TALL = """obj = C.active_object
if obj is None:
    raise RuntimeError("nothing is active")
obj.scale.z = obj.scale.z * 1.5
C.view_layer.update()
print(f"{obj.name}: z scale {obj.scale.z:.2f}")"""


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def projection_bytes(messages: list[dict]) -> int:
    return sum(
        len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for message in messages
    )


def notes(session):
    return [message for message in session.messages if message.kind == "note"]


def unanswered(history: list[dict]) -> list:
    """Ids a `tool_calls` message asked about that no `tool` message answered.

    The provider's own rule (ticket 16, HTTP 400: "must be followed by tool
    messages responding to each tool_call_id"), so a trimmed history that lost a
    result would not merely be inaccurate - it would be refused.
    """
    asked = [
        call.get("id")
        for message in history
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    answered = [
        message.get("tool_call_id") for message in history if message.get("role") == "tool"
    ]
    return [call_id for call_id in asked if call_id not in answered]


def screenshot(path: Path) -> str:
    """Save the 3D Viewport with its sidebar open. Never fatal.

    A screenshot is only evidence if it shows the thing under test, so the sidebar
    is opened and the Copilot tab selected first (measured in ticket 02's probe:
    `Region.active_panel_category` is writable only once the region has been laid
    out, so the tab is selected on a tick of its own).
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

    preferences = addon.preferences
    previous_kib = int(preferences.context_history_kib)
    preferences.context_history_kib = HISTORY_KIB
    note(f"history budget preference: {previous_kib} -> {preferences.context_history_kib} KiB")

    # A directory of this run's own, so the record the probe writes can be read
    # back without guessing which file is today's.
    history_dir = os.environ["BLENDER_COPILOT_HISTORY_DIR"]
    shutil.rmtree(history_dir, ignore_errors=True)

    # A scene this probe can make claims about, and a record long enough to trim.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
    cube = bpy.context.active_object
    for candidate in list(bpy.data.objects):
        if candidate is not cube:
            bpy.data.objects.remove(candidate, do_unlink=True)
    bpy.context.view_layer.update()

    # The conversation is filed per `.blend` path, and an unsaved file is
    # session-only by design - so the probe saves its own scene to /tmp first, which
    # is what makes the record on disk (and the check that the model's view of it is
    # smaller than the file) a real claim rather than a promise.
    bpy.ops.wm.save_as_mainfile(filepath="/tmp/bc-t06-probe.blend")
    note(f"saved a probe scene to {bpy.data.filepath}")

    session = bc.conversation.session
    session.clear()
    seed = seeded_history()
    session.history[:] = [dict(message) for message in seed]
    session.messages[:] = bc.conversation.messages_from_history(session.history)
    original = session.snapshot()
    note(
        f"seeded {len(seed)} stored messages, {projection_bytes(seed)} B, "
        f"against a {bc.stream.history_budget_bytes()} B budget"
    )

    check("the record is over its budget before anything is sent", bc.stream.history_budget_bytes() < projection_bytes(seed))
    check("and the chip is already on, because it is derived rather than stored", session.trimmed)

    worker = ScriptedWorker(
        [
            tool_round("call_1", "Scale Cube 1.5x on Z", CODE_TALL, "The cube is active, so I'll scale it."),
            prose_round("Done: the cube is 1.5x taller on Z, and the scene says so."),
        ]
    )
    bc.transport.worker = worker

    redraws: list[int] = []
    original_redraw = bc.stream.tag_view3d_redraw

    def counting_redraw() -> int:
        tagged = original_redraw()
        redraws.append(tagged)
        return tagged

    bc.stream.tag_view3d_redraw = counting_redraw

    state = {"step": 0, "turn_started": 0.0}
    budget = bc.stream.history_budget_bytes()

    def scenario() -> None:
        check("the turn was answered even though the record did not fit", not session.streaming)
        check("the cube changed, so the turn really ran", abs(cube.dimensions.z - 3.0) < 0.01)
        check(
            "nothing is left unanswered, so the history is still sendable",
            unanswered(session.history) == [],
            unanswered(session.history),
        )

        request = worker.sent[0]
        second = worker.sent[-1]
        # The live turn sits at the end of the projection and is deliberately NOT
        # charged to the budget (it is never pruned), so the budget check measures
        # what `plan` measures: the marker plus the surviving settled messages.
        # The first request carries the prompt that opened the turn, and the second
        # is the round after the tool result - so the two are asked different
        # questions rather than one being asked twice.
        live = len(original)
        live_turn = session.history[live:]
        # The turn that was newest when the request was built is protected and is
        # part of the projection without being charged to the budget, so the budget
        # check measures what `plan` measures: the head marker plus the surviving
        # *settled* messages.
        last_user = max(i for i, message in enumerate(seed) if message.get("role") == "user")
        protected_count = len(seed) - last_user
        projection = request[1:-2]
        prunable = projection[: len(projection) - protected_count]
        check(
            "two rounds went out, so the projection was rebuilt mid-turn",
            len(worker.sent) == 2,
            len(worker.sent),
        )
        check(
            "the request went out with the base prompt at index 0",
            request[0]["role"] == "system" and "Blender Copilot" in request[0]["content"],
        )
        check(
            "the live scene summary is still the last message",
            request[-1]["role"] == "system" and "captured" in request[-1]["content"],
        )
        check(
            "and the head marker sits between them",
            request[1]["role"] == "system"
            and "elided to fit the context window" in request[1]["content"],
            request[1],
        )
        check(
            "the prompt that opened the turn is still on the wire",
            request[-2] == {"role": "user", "content": "make the cube taller"},
            request[-2],
        )
        check(
            "the prunable part of the request fits the budget",
            projection_bytes(prunable) <= budget,
            (projection_bytes(prunable), budget),
        )
        check(
            "and it is a sound projection of the store as it stood then",
            bc.context.validate_projection(session.history[:live], projection) == [],
            bc.context.validate_projection(session.history[:live], projection),
        )
        check(
            "the second round is a sound projection too, turn in flight and all",
            bc.context.validate_projection(
                session.history[: live + 3], second[1:-1], protected_from=live
            )
            == [],
            bc.context.validate_projection(
                session.history[: live + 3], second[1:-1], protected_from=live
            ),
        )
        check(
            "the stored transcript is unchanged by the trim",
            session.snapshot()[: len(original)] == original,
        )
        check(
            "the record grew by the live turn alone",
            len(session.history) == len(live_turn) + len(original),
            len(session.history),
        )
        check(
            "and every seeded message is still there, byte for byte",
            json.dumps(session.history[: len(original)]) == json.dumps(original),
        )
        # The record on disk: `meta.context_trim` beside `retention`, which is what
        # ticket 14 §5 asks the file to keep. Informational only - the chip is always
        # recomputed - but a file that admitted nothing would be the silent version
        # of the same forgetting.
        written = sorted(
            path
            for path in Path(history_dir).glob("*.json")
            # The index of which file a `.blend` path maps to is not a conversation.
            if path.name != "active.json"
        )
        check("the conversation was written to a file of its own", len(written) == 1, written)
        if written:
            record = json.loads(written[0].read_text(encoding="utf-8"))
            trim = (record.get("meta") or {}).get("context_trim") or {}
            check(
                "and the file records that the model saw less than it holds",
                trim.get("last_trim", {}).get("tool_results_elided", 0) >= 1
                and bool(trim.get("first_trim_at")),
                trim,
            )
            check(
                "while keeping every stored message it held",
                record["messages"][: len(original)] == original,
            )
            note(f"meta on disk: {json.dumps(record['meta'])}")
        row = notes(session)
        check("the panel has exactly one trim note", len(row) == 1, len(row))
        check(
            "the note says what the model did not see, and that the record kept it",
            "model saw" in row[0].text and "your history is kept" in row[0].text,
        )
        check("expanding it says how much room there was", "budget" in row[0].detail)
        note(f"note text: {row[0].text}")
        for line in row[0].detail.splitlines():
            note(f"note detail: {line}")
        check("the timer repainted while the turn was in flight", bool(redraws) and any(redraws))

    def verdict() -> None:
        preferences.context_history_kib = previous_kib
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
        # A raise inside a timer does not stop Blender - it prints a traceback and
        # the next tick runs - so an unguarded probe would sit here until the bounded
        # driver's deadline killed it, with the diagnosis 150 seconds away. Catching
        # it here turns that into an ordinary failure line.
        try:
            return step_once()
        except BaseException:  # noqa: BLE001 - reported as a failure, not swallowed
            import traceback

            note("raised inside the probe:\n" + traceback.format_exc())
            FAILURES.append("the probe raised")
            verdict()
            return None

    def step_once():
        if session.streaming:
            if time.monotonic() - state["turn_started"] > TURN_DEADLINE:
                note(f"last status: {session.status!r}")
                FAILURES.append("the turn did not finish inside the deadline")
                verdict()
                return None
            return TICK

        step = state["step"]
        if step == 0:
            note(f"turn finished after {time.monotonic() - STARTED:.2f}s")
            scenario()
            # A Panel cannot scroll, so on a six-turn transcript the note is below
            # the fold and a picture of the sidebar would not show it. So: one
            # photograph of the real state (chip included), and then the transcript
            # is reduced to the note and the live turn - the same trick the undo
            # receipt needed in ticket 05 - so that the note itself is photographed
            # rather than described. Both are logged for what they are.
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 1
            return TICK

        if step == 1:
            note(f"screenshot (real state, note below the fold): {screenshot(SHOT_FULL)}")
            for message in session.messages:
                message.expanded = False
            first_note = min(
                index
                for index, message in enumerate(session.messages)
                if message.kind == "note"
            )
            session.messages[:] = session.messages[first_note:]
            note(f"transcript reduced to {len(session.messages)} rows for the photograph")
            bc.stream.tag_view3d_redraw()
            state["step"] = 2
            return TICK

        if step == 2:
            note(f"screenshot (note, collapsed): {screenshot(SHOT)}")
            for message in notes(session):
                message.expanded = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 3
            return TICK

        if step == 3:
            note(f"screenshot (note, expanded): {screenshot(SHOT_OPEN)}")
            state["step"] = 4
            return TICK

        if step == 4:
            verdict()
            return None
        return None

    preferences.prompt_text = "make the cube taller"
    result = bpy.ops.blender_copilot.send()
    note(f"send -> {result}")
    if "FINISHED" not in result:
        FAILURES.append(f"Send was refused: {result}")
    state["turn_started"] = time.monotonic()
    check("the turn opened", session.streaming)
    try:
        bpy.context.preferences.view.show_splash = False
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        note(f"could not set show_splash: {exc}")
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
