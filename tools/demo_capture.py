#!/usr/bin/env python3
"""Film a real turn in the real panel, for the pictures the README carries.

    python3 tools/bounded_run.py 240 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --window-geometry 10 10 1480 960 --python tools/demo_capture.py

**Why this is not one of the probes.** Every other `tools/*_probe.py` photographs
whatever happens to be on screen when a check runs, and that is the right thing for
evidence - `logs/loop-panel.png` shows an error block and a traceback because the
case under test *was* an error, and those pictures are how the panel's clipping
bugs were found. They are not product shots: a 300 px sidebar of a default window,
a toy cube beside it, and a camera pointed at whatever the last assertion left
behind. This file points the camera: `RING_CODE` and `PAINT_CODE` below build
something worth looking at, the window is scaled up so the transcript is legible at
the width a README displays, and a frame is written every ~0.25 s so the reply can
be watched arriving.

**What is real here and what is not.** Real: the panel, the transcript rows, the
sandbox that runs the code, the loop's one-call-per-tick cadence, the undo receipt,
and every pixel of the result. Not real: the model. `PacedWorker` stands in for
`transport.Worker`, so no request is made, no credential is read and no network is
touched - the code the ring is built from is a string in this file, handed to the
same sandbox a model's code would go to. Anyone looking at the picture is looking
at the shipped panel drawing a canned reply, and the README captions it that way.

**One event per tick, deliberately.** The assertion probes hand their whole scripted
round over on the first tick, which is exactly right for asserting the end state and
useless for filming - the reply would appear whole in a single frame. Here one
event is released per drain tick (the addon's own 0.05 s), so the transcript grows
at the rate a real stream would.

Writes a frame strip under `logs/demo/raw/`, a log, and one full-window still. It
prints `DEMO OK` or `DEMO FAILED` and quits Blender itself, so the bounded driver's
deadline stays a backstop: Blender exits 0 even when a `--python` script raises.
`tools/demo_media.py` composes the strip into the committed assets.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import bpy
from mathutils import Euler

# Dummy values so `transport.config()` is satisfied. The worker is replaced below,
# so nothing is sent and no real credential is read: `.env` is not loaded here and
# the key is a literal fake. The history dir is redirected for the same reason -
# a demo run must not land in the user's own conversation store.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-demo-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-demo")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "logs" / "demo" / "raw"
LOG = ROOT / "logs" / "demo-capture.txt"
FULL = ROOT / "logs" / "demo" / "full-window.png"

TICK = 0.05
# Frames are taken on this many harness ticks, i.e. ~4 per second. The drain runs
# at the same 0.05 s, so a frame covers ~5 events of a paced reply: enough movement
# to read as a stream, few enough that a GIF over the whole turn stays small.
FRAME_EVERY = 5
MAX_FRAMES = 120
TURN_DEADLINE = 60.0

# Resolution scale for the shot. The panel is ~300 px wide at 1.0, which is honest
# and unreadable once a README scales the picture down; at 2.0 the transcript has
# the pixels to stay legible at the width a browser shows it.
UI_SCALE = 2.0

PROMPT_1 = "make me something that looks good on a poster - twelve balls in a ring"
PROMPT_2 = "nice - now give each ball its own colour"

# The code the "model" asks for. It arrives as `function.arguments` inside a
# scripted `tool_call`; this file never runs it. Written the way the sandbox's
# prelude invites - `bpy`, `C`, `D`, `math` are already bound - and with a print at
# the end, because the panel shows the output behind the row's expander.
RING_CODE = """for stale in list(D.objects):
    D.objects.remove(stale, do_unlink=True)

count = 12
for index in range(count):
    angle = index / count * math.tau
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=1.0,
        location=(math.cos(angle) * 4.0, math.sin(angle) * 4.0, 0.0),
    )
    ball = C.active_object
    ball.name = "Ball %02d" % (index + 1)
    ball.scale = (1.0, 1.0, 0.55 + index / count * 0.9)
    ball.rotation_euler = (0.0, 0.0, angle)
C.view_layer.update()
print("%d spheres in a ring, %d objects in the file" % (count, len(D.objects)))"""

PAINT_CODE = """node_of = lambda material: next(
    node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"
)
for index, ball in enumerate(sorted(D.objects, key=lambda obj: obj.name)):
    paint = D.materials.new(name=ball.name + " Paint")
    paint.use_nodes = True
    node = node_of(paint)
    hue = index / 12.0
    node.inputs["Base Color"].default_value = (
        0.55 + 0.45 * math.cos(hue * math.tau),
        0.55 + 0.45 * math.cos(hue * math.tau + 2.1),
        0.55 + 0.45 * math.cos(hue * math.tau + 4.2),
        1.0,
    )
    node.inputs["Metallic"].default_value = 0.9
    node.inputs["Roughness"].default_value = 0.18
    ball.data.materials.clear()
    ball.data.materials.append(paint)
C.view_layer.update()
print("painted %d balls, %d materials" % (len(D.objects), len(D.materials)))"""

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(line)
    print(f"DEMO | {line}", flush=True)


def fail(line: str) -> None:
    FAILURES.append(line)
    note(f"FAILED {line}")


# ---------------------------------------------------------------------------
# The paced transport
# ---------------------------------------------------------------------------

def prose_round(text: str) -> list[dict]:
    """One assistant reply, streamed as small deltas rather than one lump."""
    words = text.split()
    events = [
        {"ev": "delta", "text": " ".join(words[start : start + 4]) + " "}
        for start in range(0, len(words), 4)
    ]
    if events:
        events[-1]["text"] = events[-1]["text"].rstrip()
    events.append({"ev": "done", "finish_reason": "stop", "tool_calls": []})
    return events


def tool_round(call_id: str, purpose: str, code: str, prose: str) -> list[dict]:
    events: list[dict] = []
    for start in range(0, len(prose.split()), 4):
        events.append({"ev": "delta", "text": " ".join(prose.split()[start : start + 4]) + " "})
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


class PacedWorker:
    """`transport.Worker`, one event per tick.

    Same methods as the real thing, and the same hand-over rule the other probes
    document: events become visible on the *next* `tick()`, because the reader
    cannot deliver mid-`send`. The difference is only how many at once - one - which
    is what turns a script into something a camera can watch.
    """

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.sent: list[dict] = []
        self._body: list[dict] = []
        self._queue: list[dict] = []
        self.cancels = 0

    @property
    def busy(self) -> bool:
        return bool(self._queue or self._body or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.sent.append({"messages": messages, "tools": tools})
        self._body.extend(self.rounds.pop(0) if self.rounds else [])
        return None

    def tick(self) -> list[dict]:
        if self._queue:
            return self._queue
        if self._body:
            self._queue = [self._body.pop(0)]
        return []

    def cancel(self) -> None:
        self.cancels += 1
        self._queue = []
        self._body = []
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def viewport_area(screen):
    return next((area for area in screen.areas if area.type == "VIEW_3D"), None)


def style_viewport() -> str:
    """Point the camera. The *scene* is the model's business; the framing is ours.

    Material Preview rather than a render: the point of the picture is the panel
    next to a scene, and a Cycles render would add a minute of wall clock and a
    denoiser's opinion to a screenshot. Overlays off, because a grid and an axis
    gizmo in the frame make it read as a viewport and not as a result.

    Each assignment is guarded on its own. One `try` around the block would let a
    single unavailable property (they differ between 5.2 and 5.3) silently skip the
    framing that matters - and the picture would come out framed by accident.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    area = viewport_area(screen) if screen else None
    if area is None:
        return "no 3D Viewport"
    space = getattr(area, "spaces", None)
    space = space.active if space else None
    region = getattr(space, "region_3d", None)
    if space is None or region is None:
        return "no space"

    wanted = [
        (lambda: setattr(space.shading, "type", "MATERIAL"), "shading=MATERIAL"),
        (lambda: setattr(space.shading, "studio_light", "basic.sl"), "studio=basic.sl"),
        (lambda: setattr(space.overlay, "show_overlays", False), "overlays off"),
        (
            lambda: setattr(region, "view_location", (0.0, 0.0, 0.7)),
            "view_location",
        ),
        (
            # A three-quarter view from slightly above: the ring reads as a ring
            # from here and as a line from the default front view.
            lambda: setattr(
                region, "view_rotation", Euler((1.20, 0.0, 0.55), "XYZ").to_quaternion()
            ),
            "view_rotation",
        ),
        (lambda: setattr(region, "view_distance", 17.0), "view_distance=17"),
        (lambda: setattr(space, "lens", 50.0), "lens=50mm"),
    ]
    done, missed = [], []
    for apply, label in wanted:
        try:
            apply()
            done.append(label)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            missed.append(f"{label}: {type(exc).__name__}: {exc}")
    area.tag_redraw()
    if missed:
        return f"styled {done}, could not set {missed}"
    return f"styled {done}"


def open_sidebar() -> str:
    """Open the sidebar. The tab is selected on a *later* tick: measured 2026-09-26,
    `Region.active_panel_category` is read-only until the region has been laid out,
    which is why the other probes split these two steps too."""
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    area = viewport_area(screen) if screen else None
    if area is None:
        return "no 3D Viewport"
    try:
        area.spaces.active.show_region_ui = True
        area.tag_redraw()
        return "sidebar opened"
    except Exception as exc:  # noqa: BLE001
        return f"could not open: {type(exc).__name__}: {exc}"


def select_tab() -> str:
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    area = viewport_area(screen) if screen else None
    if area is None:
        return "no 3D Viewport"
    region = next((r for r in area.regions if r.type == "UI"), None)
    if region is None:
        return "no sidebar region"
    try:
        region.active_panel_category = "Copilot"
        region.tag_redraw()
        return f"tab {getattr(region, 'active_panel_category', '?')!r}"
    except Exception as exc:  # noqa: BLE001
        return f"could not select: {type(exc).__name__}: {exc}"


def screenshot_area(path: Path) -> str:
    """One area, written to `path`. Never fatal: a missing frame is a smaller GIF,
    and the verdict says how many were written."""
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    area = viewport_area(screen) if screen else None
    if area is None:
        return "no 3D Viewport"
    region = next((r for r in area.regions if r.type == "UI"), None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=str(path))
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def screenshot_window(path: Path) -> str:
    """The whole window, for the still that shows the panel *inside* Blender.

    `screen.screenshot`, not `screenshot_area`: this one includes the topbar and the
    header, which is the difference between "a chat panel" and "a chat panel in
    Blender". It renders the screen's *areas* and leaves popup overlays out
    (measured for the context-view probe), so the startup splash is not in it.
    """
    window = getattr(bpy.context, "window", None)
    if window is None:
        return "no window"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001 - reported
        return f"failed: {type(exc).__name__}: {exc}"


def main() -> int:
    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        note("FAILED: no preferences - the extension is not registered as an add-on")
        return 1
    preferences = addon.preferences
    if not bpy.context.preferences.edit.use_global_undo:
        note("FAILED: Global Undo is off, so Send is disabled by design")
        return 1

    RAW.mkdir(parents=True, exist_ok=True)
    for stale in RAW.glob("*.png"):
        stale.unlink()

    session = bc.conversation.session
    session.clear()
    worker = PacedWorker(
        [
            tool_round(
                "call_1",
                "Build a ring of 12 spheres",
                RING_CODE,
                "A ring of twelve, then - each ball gets a different height so the "
                "row reads as a wave rather than a necklace. Building it now.",
            ),
            tool_round(
                "call_2",
                "Paint every ball",
                PAINT_CODE,
                "Colour next: one material per ball, stepped around the hue wheel so "
                "the ends meet. Metallic and fairly smooth, which is what makes the "
                "ring catch the studio light.",
            ),
            prose_round(
                "Done. Twelve objects added and painted, and the whole turn is one "
                "Ctrl+Z away if you would rather have your scene back."
            ),
        ]
    )
    bc.transport.worker = worker

    preferences.prompt_text = PROMPT_1
    state: dict = {"step": 0, "frames": 0, "ticks": 0, "turn_started": 0.0, "sizes": set()}

    def grab(tag: str) -> None:
        if state["frames"] >= MAX_FRAMES:
            return
        state["frames"] += 1
        path = RAW / f"f{state['frames']:03d}-{tag}.png"
        problem = screenshot_area(path)
        if problem:
            fail(f"frame {state['frames']} ({tag}): {problem}")
            return
        # The size of the *area* is the one fact that says whether `ui_scale` took
        # effect, and it is read from the file rather than assumed from the request.
        try:
            image = bpy.data.images.load(str(path))
            state["sizes"].add(tuple(image.size))
            bpy.data.images.remove(image)
        except Exception:  # noqa: BLE001 - a missing size is not a missing frame
            pass
        note(f"frame {state['frames']:03d} {tag} -> {path.name}")

    def send_turn(prompt: str) -> None:
        preferences.prompt_text = prompt
        result = bpy.ops.blender_copilot.send()
        note(f"send({prompt!r}) -> {result}")
        if "FINISHED" not in result:
            fail(f"Send refused: {result}")
        state["turn_started"] = time.monotonic()

    def verdict() -> None:
        note(f"frames written: {state['frames']}, area sizes seen: {state['sizes']}")
        note(f"tool rows: {[m.purpose for m in session.messages if m.kind == 'tool']}")
        note(f"last receipt: {session.last_receipt}")
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported
            note(f"could not write {LOG}: {exc}")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("DEMO OK" if not FAILURES else "DEMO FAILED", flush=True)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)

    def poll():
        state["ticks"] += 1
        if session.streaming:
            if time.monotonic() - state["turn_started"] > TURN_DEADLINE:
                fail(f"the turn did not finish inside {TURN_DEADLINE:.0f}s")
                verdict()
                return None
            if state["ticks"] % FRAME_EVERY == 0:
                grab("stream")
            return TICK

        step = state["step"]
        if step == 0:
            # Scale and framing first, so every frame after this one matches.
            try:
                bpy.context.preferences.view.show_splash = False
            except Exception as exc:  # noqa: BLE001
                note(f"could not set show_splash: {exc}")
            for holder in ("system", "view"):
                block = getattr(bpy.context.preferences, holder, None)
                if block is not None and hasattr(block, "ui_scale"):
                    try:
                        setattr(block, "ui_scale", UI_SCALE)
                    except Exception as exc:  # noqa: BLE001
                        note(f"could not set {holder}.ui_scale: {exc}")
            note(f"ui_scale now {bpy.context.preferences.view.ui_scale} "
                 f"/ system {bpy.context.preferences.system.ui_scale}")
            note(open_sidebar())
            state["step"] = 1
            return TICK

        if step == 1:
            note(select_tab())
            note(style_viewport())
            send_turn(PROMPT_1)
            state["step"] = 2
            return TICK

        if step == 2:
            grab("turn-1-done")
            send_turn(PROMPT_2)
            state["step"] = 3
            return TICK

        if step == 3:
            grab("final")
            note(f"full window: {screenshot_window(FULL)}")
            verdict()
            return None

        return None

    def first() -> None:
        note(f"started, tick {TICK}s, frame every {FRAME_EVERY} ticks")

    first()
    bpy.app.timers.register(poll, first_interval=TICK, persistent=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
