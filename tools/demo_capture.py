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

**Two modes, and the difference matters.**

* `DEMO_LIVE=1` — a **real provider turn**: the shipped `transport.Worker`, real
  HTTPS requests, real streaming replies, and the model's *own* `bpy` code executed
  against the live scene. This file contributes no scene code at all in this mode.
  It is **five turns**, sent one after another (`LIVE_PROMPTS`), and the recording
  runs across all of them - so what the picture shows is a conversation building
  something, not a single request being answered. It spends money (a few cents a
  run) and needs the credential in the environment, which is why it is opt-in.
* default — the same panel, the same sandbox, the same loop, with `PacedWorker`
  standing in for the transport. No key, no network, no bill, and the same pixels
  every time, which is what makes it useful for a regression.

The README's pictures are the **live** ones. A recording that replaces the model
answers a question nobody asked: the model *is* the product.

What the *harness* stages, so you can judge the claim: it empties Blender's default
file (a Cube, a Camera and a Light - the cube sits exactly where a spaceship gets
built), opens the sidebar, sets the viewport to Material Preview with overlays off,
re-frames the camera as the model adds geometry, and steps the timeline to photograph
the animation. It writes no scene code: in this mode the file below contributes no
`bpy` at all.

After the last turn the harness steps the timeline and photographs each step, if the
scene has keyframes. That is the one place a still frame cannot tell the truth - an
animation that is never played looks exactly like no animation at all.

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
from mathutils import Euler, Vector

# `DEMO_LIVE=1` records a real provider turn instead of a scripted one. Read before
# anything else, because it decides whether the fake credential below may exist at
# all.
LIVE = os.environ.get("DEMO_LIVE", "").strip() == "1"

if not LIVE:
    # Dummy values so `transport.config()` is satisfied. The worker is replaced
    # below, so nothing is sent and no real credential is read: the key is a literal
    # fake and the base URL is a port nothing listens on, so a scripted run that
    # somehow took the real path fails loudly instead of quietly billing.
    os.environ.setdefault("DEEPSEEK_API_KEY", "sk-demo-not-a-real-key")
    os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")
# Either way: a demo run must not land in the user's own conversation store.
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-demo")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "logs" / "demo" / "raw"
LOG = ROOT / "logs" / "demo-capture.txt"
FULL = ROOT / "logs" / "demo" / "full-window.png"

TICK = 0.05
# One frame per harness tick while a reply is streaming, which - because the drain
# runs on the same 0.05 s - is one frame per event. The first version ran at one
# frame per five ticks and produced six pictures of a three-second turn; the
# camera has to be as fast as the stream, not a polite fraction of it.
FRAME_EVERY = 1
# Live mode samples differently, in the same units. The reply arrives over seconds
# and the model *thinks* for tens of them, so one tick-rate camera over a 90-second
# turn writes thousands of frames of the same panel. Streaming is therefore sampled
# when the transcript actually moves, plus a 2 s heartbeat so a long think is visible
# as waiting rather than skipped.
FRAME_EVERY_LIVE = 1
FRAME_EVERY_IDLE_LIVE = 40
MAX_FRAMES = 120
# Five turns, each of which may take many rounds. The four-turn run reached 364 frames
# before it was stopped, so anything near that ceiling truncates the strip somewhere in
# the middle of the spaceship - which is to say, it loses the payoff. Frames are ~550
# KiB each, so this cap is ~500 MiB of gitignored scratch; the composer drops the
# still ones afterwards.
MAX_FRAMES_LIVE = 1400
# How many timeline steps to photograph when sweeping an animation the model built.
#
# It was 20, and that was the wrong number by a factor of twenty. The spaceship's
# animation spans 339 frames; 20 samples photographed it once every ~17 frames, and
# measured off that strip the ship's changed region moved **~300 px between every pair
# of frames** - about a quarter of the viewport. A smooth animation photographed that
# sparsely is a slideshow, which is what the first recording of it looked like.
#
# 450 is what a *smooth, ~30-second* video needs, and the two requirements agree:
#
#   * smoothness - the ship travels roughly 6000 px over the animation, so 450 samples
#     put ~13 px of movement in each frame, which the eye reads as motion;
#   * duration - 450 flight frames plus ~160 kept build frames is ~610, and played at
#     the composer's 30-second target that is ~20 fps: not a compromise, a fit.
#
# The cost is ~200 s of extra run, because the sweep is one frame per tick and a
# screenshot takes most of a second.
SWEEP_FRAMES = 450
# Rounds per turn is a levers-and-caps preference, 0 meaning the designed 8. Raised
# for the recording because a four-part modelling job is exactly the case the ticket
# that added the lever had in mind; the log says so, and `verdict` puts it back.
ROUNDS_PER_TURN_LIVE = 24
# `DEMO_CRASH_AT=<ticks>` raises a deliberate fault from inside the timer once that
# many ticks have passed. The crash path is the one piece of this tool a normal run
# never exercises, and it is also the piece whose failure mode is silent - a
# preference file left modified - which is exactly how it was discovered. So it gets a
# switch, and the switch gets tested.
CRASH_AT = int(os.environ.get("DEMO_CRASH_AT", "0") or 0)
# A live turn that takes a while is the model's business, not a fault. This is a
# backstop against a hung run, not a budget. It was 240 s, and a recording of five
# turns died on it: the fifth turn - 16 per-thruster materials, 16 flame cones, then
# re-keying the firing twice to get the coupling right - was still working at 239 s
# when the deadline fired, so the strip had four turns and no flight in it at all.
# Nothing was wrong with the model; the number was wrong.
TURN_DEADLINE = 60.0
TURN_DEADLINE_LIVE = 600.0

# Resolution scale for the shot, overridable with `DEMO_UI_SCALE` so it can be
# calibrated without editing this file. **1.0 is stock Blender** and is the value the
# published pictures use: 1.5 was tried first, to make the panel legible when a README
# scales the frame down, and it worked - at the cost of Blender's own chrome being
# 1.5x, which reads as a zoomed-in browser rather than as a Blender session. A
# screenshot of this product should look like the product.
UI_SCALE = float(os.environ.get("DEMO_UI_SCALE", "1.0"))

PROMPT_1 = "make me something that looks good on a poster - twelve balls in a ring"

# The live script, verbatim, in five turns. It is the owner's own sequence, which
# they had already run by hand and which is why it is worth recording: each prompt is
# a *conversation* step rather than a specification. "It should look scifi-y" is
# doing real work in the first one, the third - "match the scifi look, and also
# position them properly" - only means anything against the object the second one
# built, and the fifth asks for something no earlier turn has set up: flames that
# fire on the *correct* thrusters so the motion reads as intentional.
#
# Nothing here is code. Five sentences go in and `bpy` comes back, which is the whole
# claim the recording exists to support.
LIVE_PROMPTS = [
    # The first prompt has been edited twice, both times for the same reason: one
    # vague sentence left the model to invent a whole aesthetic, and it invented a
    # different one every take - a dart-like craft once, then a metal torpedo with two
    # nacelles sitting on top of the hull on black pylons, flat plates where wings
    # should be, and a blue oval stuck on the nose. The owner's addition was "and
    # aesthetically pleasing"; the clause after it names the three failures that
    # produced, because "make it look good" is not a direction.
    "Make a scifi looking spaceship. It should look scifi-y",
    "Add RCS thrusters to get 6DoF flight",
    "Make the RCS thrusters match the scifi look. And also position them properly.",
    "Give the entire Spaceship a nice flying animation. Make it smooth. Roll and sway.",
    "Add exhaust flames to main thrusters as well as RCS thrusters. animate the RCS "
    "thrusters to fire in sync with the animated motion. they should fire the correct "
    "ones so that expected motion should happen.",
]

# The scripted rounds for that one turn, in order: the loop asks for the next round
# only after a tool result has gone back, so `send()` is called once per round plus
# once for the turn itself. **One turn, not two.** The first scripted version aimed
# at two turns with three rounds between them and left the fourth request with
# nothing to hand over: the panel sat on "Waiting... 5s" until the deadline, having
# drawn a complete turn and a half, and no receipt - which is only pushed when a
# turn *ends* - ever appeared.

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

# The scripted mode keyframes the ring too, and that is not decoration: it is the
# only way the timeline sweep can be tested without spending money. The sweep is the
# one step of this tool that cannot be checked by looking at a frame - it needs a
# scene that has an animation in it - so the free mode has to have one.
SPIN_CODE = """for ball in D.objects:
    ball.keyframe_insert("rotation_euler", frame=1)
    ball.rotation_euler[2] = ball.rotation_euler[2] + math.tau
    ball.keyframe_insert("rotation_euler", frame=60)
C.view_layer.update()
print("keyframed %d balls from frame 1 to 60" % len(D.objects))"""

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
        # Hand over at most one event, and *clear* it. This is the method the first
        # run got wrong: returning `self._queue` without emptying it replayed the
        # same first delta on every tick, so the reply never advanced past its first
        # four words, no tool call was ever executed, and the run sat there until the
        # deadline - 120 frames of one frozen sentence.
        if self._queue:
            events, self._queue = self._queue, []
            return events
        if self._body:
            return [self._body.pop(0)]
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
        # `studio.exr`, and not a dark environment. In Material Preview the studio
        # HDRI is *both* the background and the light, so the tempting fix for a
        # grey background (`night.exr`) also put the ring in the dark: one framed
        # run of each, and the glossy one won. `background_type` and
        # `background_color` are accepted by the API in this shading mode and
        # ignored by it, which the run before that proved by changing nothing.
        (lambda: setattr(space.shading, "studio_light", "studio.exr"), "studio=studio.exr"),
        (lambda: setattr(space.overlay, "show_overlays", False), "overlays off"),
        (
            lambda: setattr(space.shading, "background_type", "VIEWPORT"),
            "background=viewport",
        ),
        (
            lambda: setattr(space.shading, "background_color", (0.045, 0.05, 0.07)),
            "background colour",
        ),
        (
            lambda: setattr(region, "view_location", (2.9, 0.0, 0.5)),
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
        # 26, not 17, and the target pushed 2.9 units to +x. Both numbers are about
        # the panel: at ui_scale 1.5 the sidebar takes the right third of the area,
        # and the first framed version filled the *frame* with a ring that the panel
        # then cut in half. Off-centring the camera is what puts the whole ring in
        # the part of the picture that is actually viewport.
        (lambda: setattr(region, "view_distance", 26.0), "view_distance=26"),
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


def clear_scene() -> str:
    """Empty the file before the recording starts.

    A default Blender opens with a Cube, a Camera and a Light, and the cube is at the
    origin - which is exactly where a model asked for a spaceship will build one. Left
    in place it sat *inside* the hull, showed up in the undo receipt as `objects: 3 ->
    ...` instead of naming only what the turn added, and risked a grey box in shot
    through any gap in the model's geometry. `tools/live_turn_probe.py` clears the
    scene for the same reason.

    This stages the *starting* scene and nothing else: the harness removes Blender's
    default objects, and everything in the picture after that is the model's.
    """
    names = [obj.name for obj in bpy.data.objects]
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.view_layer.update()
    return f"cleared the default file: {names or 'already empty'}"


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


def describe_window() -> str:
    """What the window actually is, in its own words.

    `--window-geometry` is a *request*: the first framed run asked for 1400x1010 and
    got an area of 1145x827, because the window manager fits the window to the
    screen it is on. Composing a crop against the number that was asked for rather
    than the number that exists is how a picture ends up with the panel half off
    the edge, so both are reported.
    """
    window = getattr(bpy.context, "window", None)
    if window is None:
        return "no window"
    screen = getattr(window, "screen", None)
    area = viewport_area(screen) if screen else None
    region = next((r for r in area.regions if r.type == "UI"), None) if area else None
    return (
        f"window {window.width}x{window.height} UI units, "
        f"viewport area {area.width}x{area.height} px, "
        # The region's own rectangle, in the same pixels as the area screenshot.
        # `tools/demo_media.py` crops against these numbers: its first version
        # guessed the panel's left edge from the pixels and picked the *tab strip*
        # boundary instead, producing a 24 px-wide sliver of a crop and calling it
        # a receipt.
        f"panel rect x={getattr(region, 'x', '?')} y={getattr(region, 'y', '?')} "
        f"w={getattr(region, 'width', '?')} h={getattr(region, 'height', '?')} "
        f"(ui_scale {bpy.context.preferences.view.ui_scale})"
    )


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


def activity(session) -> tuple:
    """A cheap ``did anything happen'' signature for the transcript.

    Deliberately not the status line: it carries an elapsed-seconds counter, so it
    changes every second of a two-minute think and would defeat the entire point of a
    signature that is supposed to stay quiet while the model is quiet.
    """
    last = session.messages[-1] if session.messages else None
    return (
        len(session.messages),
        len(getattr(last, "text", "") or ""),
        getattr(session, "running_tool", None) is not None,
    )


def action_fcurves(action) -> list:
    """Every f-curve in an action, across Blender's two action layouts.

    In 4.4/5.x an `Action` has **no `.fcurves`**: the curves moved under
    `layers -> strips -> channelbags`. So the obvious loop does not merely return
    nothing, it raises - which is exactly how the first version of this died, in a
    four-turn recording that had already been captured, at the one step that was
    supposed to show the animation. Verified against 5.2.2: `hasattr(action,
    'fcurves')` is False, `layers` is 1, the strip type is `KEYFRAME`, and the
    channelbag's f-curves carry the keyframe points.

    `getattr` rather than `hasattr` so a file written by an older release, or read by
    a newer one, still works: nothing here is RNA, so a plain look is honest.
    """
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    found: list = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for channelbag in getattr(strip, "channelbags", []):
                found.extend(channelbag.fcurves)
    return found


def animation_span(scene) -> tuple[int, int] | None:
    """The frames the model actually keyframed, read off the f-curves.

    Not `scene.frame_start`/`frame_end`: those are 1 and 250 in a default file no
    matter what was animated, so a sweep across them would spend nineteen frames out
    of twenty photographing a stationary ship and call it an animation.
    """
    low: int | None = None
    high: int | None = None
    for action in bpy.data.actions:
        for fcurve in action_fcurves(action):
            for point in getattr(fcurve, "keyframe_points", []):
                frame = int(round(point.co[0]))
                low = frame if low is None else min(low, frame)
                high = frame if high is None else max(high, frame)
    if low is None or high is None or high <= low:
        return None
    return low, high


def frame_scene(margin: float = 1.9, push: float = 0.32) -> str:
    """Point the camera at whatever is in the scene, off-centre.

    Scripted mode knows what the scripted code builds, so its camera is a constant.
    Live mode cannot: the model decides the geometry, and it may be a ring ten units
    across or a ball twenty centimetres across. So the harness frames whatever exists
    each time the object set changes - the same job a person does before taking a
    picture, and the reason it belongs here and not in the prompt.

    `push` shifts the target to +x by a fraction of the object's size, because the
    sidebar takes the right third of the frame and an object centred in the *frame*
    gets cut in half by the panel. Both numbers are fractions of the object's own
    size, which is what makes this work for a scene it has never seen.
    """
    corners: list[Vector] = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        try:
            corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        except Exception:  # noqa: BLE001 - a modifier without a mesh is not fatal
            continue
    if not corners:
        return "nothing to frame"
    low = Vector((min(c[i] for c in corners) for i in range(3)))
    high = Vector((max(c[i] for c in corners) for i in range(3)))
    centre = (low + high) / 2
    size = max(high - low)
    if size <= 0:
        return "degenerate bounds"
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    area = viewport_area(screen) if screen else None
    space = getattr(area, "spaces", None)
    space = space.active if space else None
    region = getattr(space, "region_3d", None)
    if region is None:
        return "no 3D Viewport to frame in"
    try:
        region.view_location = (centre.x + push * size, centre.y, centre.z)
        region.view_distance = margin * size
        area.tag_redraw()
    except Exception as exc:  # noqa: BLE001 - reported
        return f"could not frame: {type(exc).__name__}: {exc}"
    return (
        f"centre ({centre.x:.2f}, {centre.y:.2f}, {centre.z:.2f}), "
        f"size {size:.2f}, distance {margin * size:.2f}"
    )


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

    # Preferences are touched, so they are put back before quitting. They are read
    # live, not saved by this script - but Blender's "Auto-Save Preferences" is on
    # by default and would write whatever is in memory when the process exits, and a
    # demo must not leave the owner's Blender zoomed, tooltip-free, or carrying a turn
    # cap it never chose. (Not hypothetical: the first framed run of this tool
    # auto-saved `ui_scale = 2.0` into the owner's preferences, and putting that back
    # took a separate pass.)
    view = bpy.context.preferences.view
    state: dict = {
        "step": 0,
        "frames": 0,
        "ticks": 0,
        "turn_started": 0.0,
        "turn": 0,
        "saw_streaming": False,
        "sweep": [],
        "sweep_pending": None,
        "sweep_start": 1,
        "sizes": set(),
        "restore": [],
    }

    def remember(target, name: str) -> None:
        try:
            state["restore"].append((target, name, getattr(target, name)))
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            note(f"could not read {name} to restore it: {exc}")

    remember(view, "show_tooltips")
    remember(view, "ui_scale")
    remember(preferences, "rounds_per_turn")
    try:
        # A hover tooltip is drawn as a popup over the panel and *is* in the area
        # screenshot - the first framed run came back with "Display Filter / Live
        # search filtering string" across the transcript, because the owner's
        # pointer happened to be resting over the sidebar. There is no way to move
        # the pointer from Python in this build (`bpy.types.Window.event_simulate`
        # does not exist in 5.2.2), so the tooltips are turned off for the run.
        view.show_tooltips = False
    except Exception as exc:  # noqa: BLE001
        note(f"could not disable tooltips: {exc}")

    session = bc.conversation.session
    session.clear()
    if LIVE:
        # No substitution anywhere in this mode. `transport.worker` is the shipped
        # `Worker`: it launches Blender's own python3.13 as `_worker.py` and speaks
        # HTTPS to the provider. The code that lands in the transcript here was
        # written by the model, and no line of this file can stand in for it.
        config = bc.transport.config(preferences)
        # It returns a `Config`, not a dict - and `problem` is a sentence that
        # already names the environment variable and the preference field, so it is
        # quoted rather than re-worded here. (The first version of this check tested
        # `isinstance(config, dict)`, which is never true, so a live run with a
        # perfectly good key refused itself.)
        if getattr(config, "problem", ""):
            note(f"FAILED: {config.problem}")
            return 1
        note(
            "live mode: the shipped transport, "
            f"worker {type(bc.transport.worker).__name__}, "
            f"model {config.model!r} from {config.model_source}, "
            f"key from {config.key_source}, url from {config.url_source}"
        )
        prompts = list(LIVE_PROMPTS)
        # The turn cap is a lever (0 = the designed 8). Raised for the recording so a
        # four-part modelling job finishes in one go instead of pausing mid-build to
        # ask for "continue"; the log records it, and `verdict` puts the owner's own
        # value back.
        try:
            preferences.rounds_per_turn = ROUNDS_PER_TURN_LIVE
            note(f"rounds per turn raised to {preferences.rounds_per_turn}")
        except Exception as exc:  # noqa: BLE001 - reported
            note(f"could not raise rounds_per_turn: {exc}")
    else:
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
                tool_round(
                    "call_3",
                    "Spin the ring",
                    SPIN_CODE,
                    "Last one: a full turn over sixty frames, so the recording has "
                    "something to play at the end.",
                ),
                prose_round(
                    "Done. Twelve objects added, painted and animated, and the whole "
                    "turn is one Ctrl+Z away if you would rather have your scene back."
                ),
            ]
        )
        bc.transport.worker = worker
        prompts = [PROMPT_1]

    preferences.prompt_text = prompts[0]

    def grab(tag: str) -> None:
        if state["frames"] >= (MAX_FRAMES_LIVE if LIVE else MAX_FRAMES):
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
        cap = MAX_FRAMES_LIVE if LIVE else MAX_FRAMES
        note(f"frames written: {state['frames']} (cap {cap}), sizes {state['sizes']}")
        if state["frames"] >= cap:
            note("the frame cap was reached - the strip ends before the run did")
        note(f"tool rows: {[m.purpose for m in session.messages if m.kind == 'tool']}")
        note(f"last receipt: {session.last_receipt}")
        if LIVE:
            # The provider's own numbers, which is the only thing that distinguishes
            # a real turn from a convincing recording of one.
            note(f"provider usage: {getattr(session, 'usage', None)}")
            note(f"final status: {session.status!r}")
            problem = getattr(session, "transport_error", None)
            if problem:
                fail(f"the transport reported an error: {problem}")
            elif not session.last_receipt:
                fail("the turn changed nothing, so there is no receipt to photograph")
        for target, name, value in state["restore"]:
            try:
                setattr(target, name, value)
                note(f"restored {name} = {value}")
            except Exception as exc:  # noqa: BLE001 - reported
                note(f"could not restore {name}: {exc}")
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

    def prepare_sweep() -> str:
        """Decide which frames to photograph, and loosen the camera for the travel.

        The keyframes decide the span. The camera is widened a little first, because
        a shot framed tight on a parked ship will not hold a ship that flies.
        """
        scene = bpy.context.scene
        span = animation_span(scene)
        state["sweep_start"] = int(scene.frame_current)
        if span is None:
            return "no keyframes anywhere, so there is nothing to play"
        low, high = span
        count = min(SWEEP_FRAMES, high - low + 1)
        frames = (
            [low + round((high - low) * index / (count - 1)) for index in range(count)]
            if count > 1
            else [low]
        )
        state["sweep"] = frames
        state["sweep_start"] = low
        note(f"scene fps: {int(scene.render.fps)}")
        note(f"widened for travel: {frame_scene(margin=2.4)}")
        return f"frames {low}-{high}, photographing {len(frames)} of them"

    def step():
        state["ticks"] += 1
        if CRASH_AT and state["ticks"] == CRASH_AT:
            raise RuntimeError(
                f"DEMO_CRASH_AT={CRASH_AT}: deliberate fault, to exercise the restore"
            )
        deadline = TURN_DEADLINE_LIVE if LIVE else TURN_DEADLINE

        if session.streaming:
            state["saw_streaming"] = True
            if time.monotonic() - state["turn_started"] > deadline:
                fail(
                    f"turn {state['turn'] + 1} of {len(prompts)} did not finish inside "
                    f"{deadline:.0f}s"
                )
                verdict()
                return None
            # Re-frame whenever the object set changes, in both modes. In live mode
            # the model decides the geometry and the harness cannot know it in
            # advance; in scripted mode it is the same rule for consistency, and it
            # reproduces the constant this used to be (a ring ten units across frames
            # at distance 26 with the target pushed 3 units to +x) to within 0.4.
            scene_key = tuple(
                sorted(obj.name for obj in bpy.data.objects if obj.type == "MESH")
            )
            if scene_key != state.get("scene_key"):
                state["scene_key"] = scene_key
                note(f"reframe for {len(scene_key)} objects: {frame_scene()}")
            # A frame when the transcript moved, plus a slow heartbeat, so a long
            # think shows up as waiting instead of as six hundred identical pictures.
            # Both cadences collapse to "every tick" in scripted mode.
            signature = activity(session)
            moved = signature != state.get("signature")
            state["signature"] = signature
            stream_every = FRAME_EVERY_LIVE if LIVE else FRAME_EVERY
            beat_every = FRAME_EVERY_IDLE_LIVE if LIVE else 1
            if (moved and state["ticks"] % stream_every == 0) or (
                state["ticks"] % beat_every == 0
            ):
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
            note(clear_scene())
            note(open_sidebar())
            state["step"] = 1
            return TICK

        if step == 1:
            note(select_tab())
            note(style_viewport())
            note(describe_window())
            send_turn(prompts[0])
            state["step"] = 2
            return TICK

        if step == 2:
            # A turn has ended. `saw_streaming` guards the hand-off: the tick after a
            # send is not yet streaming, so without it the script would advance before
            # the turn it had just asked for had begun.
            if not state["saw_streaming"]:
                if time.monotonic() - state["turn_started"] > deadline:
                    fail("the turn never started - no streaming state was ever seen")
                    verdict()
                    return None
                return TICK
            state["saw_streaming"] = False
            state["turn"] += 1
            if state["turn"] < len(prompts):
                note(f"turn {state['turn']} of {len(prompts)} finished")
                send_turn(prompts[state["turn"]])
                return TICK
            note(f"all {len(prompts)} turns finished")
            note(f"sweep: {prepare_sweep()}")
            state["step"] = 3
            return TICK

        if step == 3:
            # The animation, which is the one thing a still frame cannot show: the
            # model's last turn ends with the ship parked on frame 1, looking exactly
            # like a ship with no animation at all. So the harness steps the timeline
            # and photographs each step.
            #
            # Deliberately not `screen.animation_play()`: playback is real time, which
            # would make the pictures depend on how fast this machine happens to draw.
            # A frame is set on one tick and photographed on the next, because the
            # area has to be redrawn before a screenshot can show the new pose.
            if state["sweep_pending"] is not None:
                grab(f"sweep{state['sweep_pending']:03d}")
                state["sweep_pending"] = None
                return TICK
            if state["sweep"]:
                frame = state["sweep"].pop(0)
                bpy.context.scene.frame_set(frame)
                state["sweep_pending"] = frame
                return TICK
            bpy.context.scene.frame_set(state["sweep_start"])
            note(f"timeline returned to frame {state['sweep_start']}")
            state["step"] = 4
            return TICK

        if step == 4:
            # One last framing, so the still is composed for what was finally built
            # rather than for an intermediate stage of the conversation.
            note(f"final reframe: {frame_scene()}")
            grab("final")
            note(f"full window: {screenshot_window(FULL)}")
            verdict()
            return None

        return None

    def poll():
        """The timer: the step machine, and a preference restore if it ever raises.

        The crash path is not hypothetical. The sweep raised on a Blender 5.2 API
        change *after* a four-turn recording had been captured; the timer died with
        it, nothing called `verdict`, and the values this run had modified - tooltips
        off, the turn cap raised - were still in memory when Blender next auto-saved
        preferences. So one demo run left the owner's Blender changed, and a separate
        pass was needed to put it back. Restoring here means any exception puts the
        preferences back *before* the traceback finishes printing.
        """
        try:
            return step()
        except BaseException:
            import traceback

            traceback.print_exc()
            fail("the capture crashed - preferences restored before quitting")
            verdict()
            return None

    def first() -> None:
        note(
            f"started in {'LIVE (real provider)' if LIVE else 'scripted'} mode, "
            f"tick {TICK}s, frames every {FRAME_EVERY if not LIVE else FRAME_EVERY_LIVE} "
            f"ticks while the transcript moves, "
            f"{1 if not LIVE else FRAME_EVERY_IDLE_LIVE} otherwise, "
            f"max {MAX_FRAMES if not LIVE else MAX_FRAMES_LIVE}"
        )

    first()
    bpy.app.timers.register(poll, first_interval=TICK, persistent=True)
    return 0


if __name__ == "__main__":
    # The house wrapper. `main()` returning 0 leaves Blender running on the timer
    # it just registered; only a non-zero code exits, which is why an early
    # `return 1` here is a real failure rather than a silent no-op. The first
    # version of this file raised `SystemExit(main())` instead and Blender quit
    # 1.1 s in, having printed one line and taken no frames at all.
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("DEMO FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
