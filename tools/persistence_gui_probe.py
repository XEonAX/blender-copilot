"""Two GUI sessions: one that chats, one that opens the file again after a restart.

    BLENDER_COPILOT_HISTORY_DIR=/tmp/bc-t04-gui/conversations \\
        python3 tools/bounded_run.py 90 -- /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/persistence_gui_probe.py -- --stage 1
    ... --stage 2

Run in a **window**, not `-b`. Three things here need one, and each is a reason
this probe exists at all:

  * `bpy.app.timers` never pump in background mode, so the *real* event loop is the
    only way to see the turn-end write happen through `stream._tick` rather than
    through a script calling `persist()` by hand;
  * a screenshot is the only evidence for what the panel says about the scope, and
    the panel's own draw cannot run without a window;
  * `invoke_confirm` opens a modal popup, which needs a screen.

Two processes rather than one, because the ticket's first line is "a conversation
survives closing and reopening Blender": stage 1 chats and quits Blender itself,
stage 2 starts a *new* Blender, opens the same file, and looks for the thread.

It writes `logs/persistence-gui.txt` line by line (so a killed run still leaves its
evidence), `logs/persistence-stage{1,2}.png` and `logs/persistence-confirm.png`,
prints a token, and quits Blender itself.

The extension is **not** registered by this script: a GUI launch reads the user's
preferences, where `bl_ext.user_default.blender_copilot` is already enabled. That
also means the probe is running the installed extension's own `__package__`, which
is the only way `extension_path_user` gives a real directory.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import bpy

# Dummy values: the worker object is replaced below, so nothing is sent and no
# real credential is read. Without them the loop's second round is refused.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "persistence-gui.txt"
SHOTS = ROOT / "logs"
TICK = 0.1
TURN_DEADLINE = 20.0

SCRATCH = Path("/tmp/bc-t04-gui")
BLEND = SCRATCH / "persist-gui.blend"
SENTINEL = "SENTINEL-gui-turn-one"

STAGE = 1
if "--" in sys.argv:
    rest = sys.argv[sys.argv.index("--") + 1:]
    for index, item in enumerate(rest):
        if item == "--stage" and index + 1 < len(rest):
            STAGE = int(rest[index + 1])

FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    print(f"GUI | {line}", flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return bool(condition)


class ScriptedWorker:
    """The worker protocol, as in `tools/loop_panel_probe.py`."""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
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
        self._queue = []


def rounds_for_a_turn() -> list[list[dict]]:
    return [
        [
            {"ev": "delta", "text": SENTINEL},
            {
                "ev": "done",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "gui_1",
                        "type": "function",
                        "function": {
                            "name": "run_blender_python",
                            "arguments": json.dumps(
                                {"code": "print('gui probe ran this')", "purpose": "Say hello"}
                            ),
                        },
                    }
                ],
            },
        ],
        [
            {"ev": "delta", "text": "Done: the panel ran the call."},
            {"ev": "done", "finish_reason": "stop", "tool_calls": []},
        ],
    ]


def screenshot(name: str) -> str:
    """Save the 3D Viewport with its sidebar open and the Copilot tab showing."""
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
    note(
        f"     geometry: area={area.width}x{area.height} region={region.width}x{region.height} "
        f"alignment={region.alignment!r} show_region_ui={area.spaces.active.show_region_ui} "
        f"category={region.active_panel_category!r}"
    )
    if not region.active_panel_category:
        return f"sidebar has no category selected ({region.active_panel_category!r})"
    path = SHOTS / name
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=str(path))
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return f"failed: {type(exc).__name__}: {exc}"
    return f"{path.name}, sidebar tab {region.active_panel_category!r}"


def open_sidebar(bc) -> None:
    """Show the sidebar, and tag a redraw.

    Both tags matter, and the timing matters more: `show_region_ui` changes the
    area's layout, but a screenshot captures what was *drawn*, so the sidebar has
    to be open long before the picture is taken. Three ticks was not enough - the
    first version of this probe produced three pictures of a full-width viewport
    while the log reported `show_region_ui=True`, `region=335x775` and the right
    category. So this is called once at the start of the run, not just before the
    shot.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.show_region_ui = True
            area.tag_redraw()
            for region in area.regions:
                region.tag_redraw()
    bc.stream.tag_view3d_redraw()


def quit_with_verdict() -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            for failure in FAILURES:
                handle.write(f"FAILED: {failure}\n")
    except OSError:
        pass
    print("SMOKE OK" if not FAILURES else "SMOKE FAILED", flush=True)
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        sys.exit(0 if not FAILURES else 1)


def main() -> int:
    bc = importlib.import_module(EXT)
    scope = bc.scope
    store = importlib.import_module(f"{EXT}.store")
    session = bc.conversation.session

    note(f"stage {STAGE} | blender {bpy.app.version_string} | {time.strftime('%H:%M:%S')}")
    note(f"scope handlers live in this session: load_post={scope._on_load_post in bpy.app.handlers.load_post} "
         f"save_post={scope._on_save_post in bpy.app.handlers.save_post} "
         f"save_pre={scope._on_save_pre in bpy.app.handlers.save_pre}")
    check(
        "the handlers are live in a real GUI session, not just in a script that added them",
        scope._on_load_post in bpy.app.handlers.load_post,
    )
    note(f"history directory: {scope.store_.directory()}")

    if STAGE == 1:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
        # Opened here, seconds before the first picture, because the sidebar's
        # presence in a *screenshot* lags the layout change that put it there.
        open_sidebar(bc)

        worker = ScriptedWorker(rounds_for_a_turn())
        bc.transport.worker = worker
        addon = bpy.context.preferences.addons[EXT]
        addon.preferences.prompt_text = SENTINEL
        state = {"step": 0, "started_at": None, "screenshot": ""}

        def selected_tab() -> None:
            """Select the Copilot tab. Only accepted once the region is laid out."""
            window = getattr(bpy.context, "window", None)
            screen = getattr(window, "screen", None) if window else None
            for area in screen.areas if screen else []:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "UI":
                        try:
                            region.active_panel_category = "Copilot"
                        except Exception as exc:  # noqa: BLE001 - reported
                            note(f"could not select the Copilot tab: {exc}")

        def stage1_poll():
            if state["started_at"] is not None and session.streaming:
                if time.monotonic() - state["started_at"] > TURN_DEADLINE:
                    FAILURES.append("the turn did not finish inside the deadline")
                    note(f"last status: {session.status!r}")
                    quit_with_verdict()
                    return None
                return TICK

            step = state["step"]
            if step == 0:
                # Still unsaved: the state the ticket asks the panel to be honest
                # about, photographed before anything is written.
                check(
                    "an unsaved file says its conversation lasts the session only",
                    scope.header() == store.SESSION_ONLY_LABEL and "nothing is written" in scope.note(),
                )
                state["step"] = 1
                return TICK

            if step == 1:
                selected_tab()
                state["step"] = 2
                return TICK

            if step == 2:
                bc.stream.tag_view3d_redraw()
                note(f"unsaved screenshot: {screenshot('persistence-unsaved.png')}")
                bpy.ops.wm.save_as_mainfile(filepath=str(BLEND), compress=False)
                check("the header names the file that was just saved", scope.header() == BLEND.name)
                if bpy.ops.blender_copilot.send() != {"FINISHED"}:
                    FAILURES.append("Send was refused")
                state["started_at"] = time.monotonic()
                state["step"] = 3
                return TICK

            if step == 3:
                note(f"turn finished after {time.monotonic() - STARTED:.2f}s, status {session.status!r}")
                check("the turn ended", not session.streaming)
                files = sorted(name for name in os.listdir(scope.store_.directory()) if name.endswith(".json"))
                note(f"history after the turn: {files}")
                check(
                    "the real event loop wrote the conversation on the tick the turn ended",
                    len(files) == 2 and scope.current.id,
                )
                stored = json.loads(
                    (Path(scope.store_.directory()) / f"{scope.current.id}.json").read_text(encoding="utf-8")
                )["messages"]
                check("with the turn's wire messages", stored == session.history and len(stored) == 4)
                check("the sentinel is on disk, so stage 2 has something to find",
                      SENTINEL in json.dumps(stored))
                state["step"] = 4
                return TICK

            if step == 4:
                state["step"] = 5
                return TICK

            if step == 5:
                bc.stream.tag_view3d_redraw()
                state["screenshot"] = screenshot("persistence-stage1.png")
                note(f"screenshot: {state['screenshot']}")
                check("a panel screenshot was taken", "persistence-stage1.png" in state["screenshot"])
                check("and the sidebar shows the Copilot tab", "Copilot" in state["screenshot"])
                note("the conversation is on disk and Blender is about to quit; stage 2 starts a "
                     "new process and opens the same file")
                quit_with_verdict()
                return None
            return None

            return None

        bpy.app.timers.register(stage1_poll, first_interval=TICK)
        return 0

    # ------------------------------------------------------------------ stage 2
    check("a new Blender starts with an empty conversation", session.history == [])
    bpy.ops.wm.open_mainfile(filepath=str(BLEND))
    check("opening the saved file switched the scope to it", scope.current.blend_path == str(BLEND))
    check("and the conversation came back", session.history != [] and session.messages != [])
    note(f"restored {len(session.history)} wire messages, {len(session.messages)} rows")
    check("the sentinel turn is among them", SENTINEL in json.dumps(session.history))
    kinds = [row.kind for row in session.messages]
    check("the rows were rebuilt from the wire", kinds == ["user", "assistant", "code", "tool", "assistant"])
    check("the tool row is finished, not running", session.messages[3].status == "ok")
    check("the header names the reopened file", scope.header() == BLEND.name)
    check(
        "and the store is the real one, under the extension's user directory tree or the override",
        bool(scope.store_.directory()),
    )
    open_sidebar(bc)

    state2 = {"step": 0, "confirm": ""}

    def stage2_poll():
        if session.streaming:
            return TICK
        step = state2["step"]
        if step == 0:
            open_sidebar(bc)
            state2["step"] = 1
            return TICK
        if step == 1:
            window = getattr(bpy.context, "window", None)
            screen = getattr(window, "screen", None) if window else None
            for area in screen.areas if screen else []:
                if area.type != "VIEW_3D":
                    continue
                for region in area.regions:
                    if region.type == "UI":
                        try:
                            region.active_panel_category = "Copilot"
                        except Exception as exc:  # noqa: BLE001 - reported
                            note(f"could not select the Copilot tab: {exc}")
            state2["step"] = 2
            return TICK
        if step == 2:
            bc.stream.tag_view3d_redraw()
            state2["step"] = 3
            return TICK
        if step == 3:
            bc.stream.tag_view3d_redraw()
            note(f"panel screenshot: {screenshot('persistence-stage2.png')}")
            state2["step"] = 4
            return TICK
        if step == 4:
            # The confirmation, in a real window. `INVOKE_DEFAULT` is the point:
            # a bare `bpy.ops....()` runs `execute` and skips `invoke`, so the
            # panel button's own path - ask first, delete on the answer - is only
            # exercised when invoke is demanded. The file list is remembered in
            # `state2` because each tick is a *new* call of this function; a local
            # would be gone by the tick that checks it.
            state2["files_before"] = sorted(
                name for name in os.listdir(scope.store_.directory()) if name.endswith(".json")
            )
            note(f"conversations before: {state2['files_before']}")
            try:
                result = bpy.ops.blender_copilot.delete_history("INVOKE_DEFAULT")
            except Exception as exc:  # noqa: BLE001 - reported
                note(f"delete-history invoke raised: {type(exc).__name__}: {exc}")
                FAILURES.append("delete-history could not be invoked")
                quit_with_verdict()
                return None
            note(f"bpy.ops.blender_copilot.delete_history('INVOKE_DEFAULT') -> {result}")
            if set(result) != {"RUNNING_MODAL"}:
                FAILURES.append(f"delete-all did not stop for confirmation: {result}")
            state2["step"] = 5
            return TICK
        if step == 5:
            bc.stream.tag_view3d_redraw()
            note(f"confirmation screenshot: {screenshot('persistence-confirm.png')}")
            files_after = sorted(
                name for name in os.listdir(scope.store_.directory()) if name.endswith(".json")
            )
            check(
                "nothing was deleted while the confirmation is unanswered",
                files_after == state2["files_before"],
            )
            quit_with_verdict()
            return None
        return None

    bpy.app.timers.register(stage2_poll, first_interval=TICK)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        note(traceback.format_exc())
        print("SMOKE FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
