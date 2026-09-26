#!/usr/bin/env python3
"""The acceptance sentence, against the real provider, through the real panel.

    set -a; . ./.env; set +a     # the shell that launches this MUST have done so
    python3 tools/bounded_run.py 300 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/live_turn_probe.py

**Nothing here is faked.** No scripted worker, no localhost provider: the
transport is `transport.Worker`, the model is DeepSeek, and the credential comes
from wherever `transport.config()` finds it - preference first, environment
second. It is the first harness in this effort that drives a *real* model through
the loop; every other loop probe (`tools/loop_wire_probe.py`,
`tools/loop_panel_probe.py`) stands a scripted one in for the model's decision,
deliberately, so that a failure cannot be blamed on the provider.

That is also why it is expensive relative to the rest of the suite: it spends a
few cents per run. It is not to be looped on failure. Read
`logs/live-turn.txt` - which carries the whole transcript, including the model's
code and the tool envelopes - and fix the cause.

What it asserts, and deliberately does **not** assert:

  * the turn ends, with no transport error, and the transcript holds a code row
    and a tool row;
  * the scene gained geometry that was not there before, and among the materials
    now in the scene there is one whose colour reads red-ish and one that reads
    yellow-ish - read from base colours, not from names, because a material called
    "Red" proves nothing;
  * one undo (`bpy.ops.ed.undo()`, the operator Ctrl+Z invokes) takes the whole
    turn back;
  * a screenshot is written and its path is printed.

What it does not assert: that the football looks like a football. That is a
judgement about the model's code, not about the loop, and the line between the two
is the interesting part of its report. Nor does it assert anything about the
prose the model wrote - the model's prose is not evidence, the scene is.

It writes `logs/live-turn.txt`, tries to save `logs/live-turn.png`, prints
`LIVE OK` / `LIVE FAILED`, and quits Blender itself so the bounded driver's
deadline is a backstop rather than the mechanism. `AGENTS.md`: Blender exits 0
even when a `--python` script raises, so **grep for the token** - the exit status
is not the verdict.
"""

from __future__ import annotations

import colorsys
import importlib
import os
import sys
import time
from collections import Counter
from pathlib import Path

import bpy
from mathutils import Vector

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "live-turn.txt"
SHOT = ROOT / "logs" / "live-turn.png"

# The acceptance sentence, verbatim and character for character. If it changes
# here it is no longer the acceptance test.
SENTENCE = "Generate me a Football of Red and yellow colors instead of black and white"

TICK = 0.1
TURN_DEADLINE = 240.0

# The probes' conversations must not land in the user's real extension directory.
# Set BEFORE the add-on is imported, because `scope.py` reads it at call time but
# the first call can happen during registration.
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-live-probe")

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(line)
    print(f"LIVE | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


# ---------------------------------------------------------------------------
# Scene reading. Colours, never names.
# ---------------------------------------------------------------------------

def material_colours() -> list[tuple[str, str, str, list]]:
    """`(object, material, route, colour)` for every colour a material can draw with.

    Three routes, because "the material is red" has three places to hide and a
    check that reads only one of them reports a false failure: the viewport
    display colour, the Principled BSDF's unlinked Base Color (what a render
    actually uses), and the literal colours inside the node tree - an `RGB` node
    or a colour ramp's stops, which is how a two-tone ball is often built. A
    *linked* Base Color has no single colour to read, so the nodes feeding it are
    collected instead and the log says which route was found.
    """
    found: list[tuple[str, str, str, list]] = []
    colour_attrs: list[str] = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for attribute in obj.data.color_attributes:
            colour_attrs.append(f"{obj.name}/{attribute.name}")
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            found.append((obj.name, material.name, "viewport", list(material.diffuse_color)))
            tree = material.node_tree if material.use_nodes else None
            if tree is None:
                continue
            for node in tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    socket = node.inputs.get("Base Color")
                    if socket is not None and not socket.is_linked:
                        found.append((obj.name, material.name, "base", list(socket.default_value)))
                elif node.type == "RGB":
                    output = node.outputs.get("Color")
                    if output is not None:
                        found.append((obj.name, material.name, "node", list(output.default_value)))
                elif node.type == "VALTORGB":
                    ramp = node.color_ramp
                    if ramp is not None:
                        for element in ramp.elements:
                            found.append(
                                (obj.name, material.name, "ramp", list(element.color))
                            )
    if colour_attrs:
        note(f"colour attributes present: {', '.join(colour_attrs)}")
    return found


def hue_label(rgba) -> str:
    """Which band a colour falls in, in hue degrees.

    The bands are the ones a human would use when asked "is that red?" - a
    saturated, non-dark hue at the red end or the yellow end. Orange sits between
    them and is reported as its own label rather than being stretched into either,
    because "the model made an orange ball" is a real finding and not a pass.

    The yellow band is wide on purpose. The first live run's ball came back
    `(0.95, 0.72, 0.05)` - hue 45, saturation 0.95, which is a saturated gold that
    any human calls yellow - and a 50-degree floor called it orange and failed the
    run. The complaint was about the check, not the model: orange means the
    20-35 degree band, where red is still visibly mixed in.
    """
    red, green, blue = (float(channel) for channel in rgba[:3])
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    degrees = round(hue * 360.0)
    if saturation < 0.30 or value < 0.15:
        return f"neutral (hue {degrees}, sat {saturation:.2f}, val {value:.2f})"
    if degrees <= 20 or degrees >= 340:
        return f"red-ish (hue {degrees}, sat {saturation:.2f})"
    if 20 < degrees < 35:
        return f"orange (hue {degrees}, sat {saturation:.2f})"
    if 35 <= degrees <= 75:
        return f"yellow-ish (hue {degrees}, sat {saturation:.2f})"
    return f"other (hue {degrees}, sat {saturation:.2f})"


def red_ish(rgba) -> bool:
    return hue_label(rgba).startswith("red-ish")


def yellow_ish(rgba) -> bool:
    return hue_label(rgba).startswith("yellow-ish")


def frame_view(obj) -> str:
    """Point the 3D Viewport at `obj`, so the picture shows what the turn made.

    Set through `region_3d` rather than `bpy.ops.view3d.view_selected()`: the
    operator needs the area's own context and its poll depends on what happens to
    be selected, while these two attributes are deterministic. Worth measuring
    rather than assuming - the default view leaves a 0.21 m ball a speck at the
    origin, and the first live screenshot showed exactly that: the ball was
    identifiable only by its outline against the grid.
    """
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return "no window"
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        return "no 3D Viewport"
    region_3d = getattr(area.spaces.active, "region_3d", None)
    if region_3d is None:
        return "no region_3d"
    bpy.context.view_layer.update()
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    center = sum(corners, Vector()) / 8.0
    size = max(obj.dimensions) or 1.0
    region_3d.view_location = center
    region_3d.view_distance = size * 2.6
    area.tag_redraw()
    return (
        f"framed {obj.name} at {tuple(round(axis, 3) for axis in center)}, "
        f"view distance {round(size * 2.6, 3)}"
    )


def mesh_report(obj) -> list[str]:
    """What the ball is made of, as numbers rather than as the model's prose.

    Reported and deliberately **not asserted**: a football is 12 pentagons and 20
    hexagons on a sphere, but a model could legitimately build one some other way
    and this probe has no business failing a turn for choosing a different
    construction. The histogram is here so the judgement "does it look like a
    football" has something to rest on that is not the model's own summary.
    """
    lines = []
    mesh = obj.data
    sizes = Counter(len(polygon.vertices) for polygon in mesh.polygons)
    lines.append(
        "face sizes: "
        + ", ".join(f"{count}x{size}gon" for size, count in sorted(sizes.items()))
    )
    by_material = Counter(polygon.material_index for polygon in mesh.polygons)
    slots = [slot.material.name if slot.material else "(none)" for slot in obj.material_slots]
    lines.append(f"materials: {slots}")
    lines.append(
        "faces per material: "
        + ", ".join(
            f"slot {index} ({slots[index] if index < len(slots) else '?'}) = {count}"
            for index, count in sorted(by_material.items())
        )
    )
    return lines


def screenshot(path: Path) -> str:
    """Save the 3D Viewport with its sidebar open. Never fatal.

    The sidebar is opened and the Copilot tab selected before this runs, because
    a picture of a closed sidebar proves nothing about the panel. MEASURED
    2026-09-26 (5.2.2, by `tools/loop_panel_probe.py`):
    `Region.active_panel_category` is writable only once the region has been laid
    out, so the tab is selected on a later timer tick than the one that opened the
    sidebar.
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


# ---------------------------------------------------------------------------
# The transcript, for the failure analysis this probe exists to feed.
# ---------------------------------------------------------------------------

def dump_transcript(session) -> None:
    note("--- transcript ---")
    for message in session.messages:
        kind = message.kind
        if kind == "assistant":
            text = (message.text or "").strip()
            if text:
                for line in text.splitlines():
                    note(f"[assistant] {line}")
            continue
        note(f"[{kind}] {message.purpose or ''} status={message.status or ''}")
        detail = (message.detail or "").strip()
        if detail:
            for line in detail.splitlines():
                note(f"    {line}")
    note("--- end transcript ---")


def main() -> int:
    # Before anything is spent: is there a credential at all, and is it a real
    # endpoint rather than the dummy the cheap probes wire in? A missing `.env`
    # here would otherwise show up as an auth failure, which is a much more
    # expensive way to learn the same thing.
    if not os.environ.get("DEEPSEEK_API_KEY"):
        note("FAILED: no DEEPSEEK_API_KEY in this process' environment")
        note("this probe makes a real request; the launching shell must have run:")
        note("    set -a; . ./.env; set +a")
        FAILURES.append("no credential")
        return 1
    url = (os.environ.get("DEEPSEEK_API_URL") or "").strip()
    if not url:
        note("FAILED: no DEEPSEEK_API_URL in this process' environment")
        FAILURES.append("no base URL")
        return 1
    if "127.0.0.1" in url or url.endswith(":9"):
        note(f"FAILED: DEEPSEEK_API_URL is {url!r}, which is the dummy the faking probes use")
        FAILURES.append("dummy endpoint")
        return 1

    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        note("FAILED: no preferences - the extension is not registered as an add-on")
        return 1
    settings = addon.preferences
    if not bpy.context.preferences.edit.use_global_undo:
        note("FAILED: Global Undo is off, so Send is disabled by design")
        note("turn it on in Edit > Preferences > System, then re-run")
        return 1

    # Which route the credential actually came from, as fingerprints only - never
    # the value. Preference-first is the layering under test, so this is the line
    # that says whether the run exercised the environment or the field.
    config = bc.transport.config(settings)
    note(f"credential: {config.describe()}")
    note(
        f"per-request output ceiling: {config.max_tokens} tokens "
        f"(model {config.model!r})"
    )
    if config.problem:
        note(f"FAILED: transport.config() says: {config.problem}")
        FAILURES.append("transport not configured")
        return 1
    note(f"endpoint {config.base_url} - a real request to the real provider is about to be made")

    # A scene this probe can make claims about: empty, so every object after the
    # turn is one the turn created and the before/after comparison is the whole of
    # the evidence. The user's own startup file is not trusted for that.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for candidate in list(bpy.data.objects):
        bpy.data.objects.remove(candidate, do_unlink=True)
    bpy.context.view_layer.update()
    # A marker for the prepared scene, and not decoration: operator calls made
    # from Python never push undo (AGENTS.md), so without this the probe's own
    # deletions would be invisible to the stack and one undo would land on the
    # state the *file* was loaded in - the startup cube, camera and light - which
    # is both a wrong comparison and a confusing one. Measured on the first live
    # run: the turn created nothing, and the single undo restored
    # ['Camera', 'Cube', 'Light'].
    pushed = bpy.ops.ed.undo_push(message="copilot live probe: scene prepared")
    note(f"scene prepared: {len(bpy.data.objects)} objects before the turn, marker {pushed}")
    before = {obj.name for obj in bpy.data.objects}

    # The real worker, deliberately NOT replaced: this is the whole point.
    session = bc.conversation.session
    session.clear()
    state = {"step": 0, "turn_started": 0.0, "traces": 0, "ball": None, "strip": []}

    def send_turn(text: str) -> None:
        settings.prompt_text = text
        result = bpy.ops.blender_copilot.send()
        note(f"send({text!r}) -> {result}")
        if "FINISHED" not in result:
            FAILURES.append(f"Send refused: {result}")
        state["turn_started"] = time.monotonic()

    def verify_turn() -> None:
        """Everything the turn itself is evidence for, before the undo."""
        elapsed = time.monotonic() - state["turn_started"]
        note(f"the turn finished after {elapsed:.1f}s")

        transport_error = session.transport_error
        check("the turn ended", not session.streaming and session.phase == "idle")
        check("with no transport error", not transport_error)
        # The working strip is drawn whenever a turn is in flight, so a real turn
        # that was sampled while in flight is evidence that a human watching this
        # saw something moving. What the samples say is the report's business: the
        # phases a real model goes through, which no scripted worker can answer.
        check("the strip was drawn while the real turn ran", bool(state["strip"]))
        note("strip timeline: " + " | ".join(state["strip"]))
        if transport_error:
            note(f"transport error: {transport_error}")

        codes = [m for m in session.messages if m.kind == "code"]
        tools = [m for m in session.messages if m.kind == "tool"]
        errors = [m for m in session.messages if m.kind == "error"]
        check("the transcript holds a code row", len(codes) >= 1)
        check("and a tool row", len(tools) >= 1)
        check("and at least one call actually ran and succeeded",
              any(m.status == "ok" for m in tools))
        note(f"rows: {len(codes)} code, {len(tools)} tool, {len(errors)} error")
        for message in tools:
            note(f"tool row: {message.purpose!r} status={message.status}")
        for message in errors:
            note(f"error row: {(message.text or '')[:200]!r}")

        # Geometry that was not there before. A mesh with faces, not just an empty
        # added by a confused model.
        after = list(bpy.data.objects)
        gained = [obj for obj in after if obj.name not in before]
        note(
            "objects gained: "
            + ", ".join(f"{obj.name} ({obj.type})" for obj in gained)
        )
        check("the scene gained at least one object", bool(gained))
        meshes = [obj for obj in gained if obj.type == "MESH" and len(obj.data.polygons)]
        check("and it is a mesh with faces rather than an empty", bool(meshes))
        if meshes:
            obj = meshes[0]
            state["ball"] = obj
            note(
                f"{obj.name}: {len(obj.data.vertices)} vertices, "
                f"{len(obj.data.polygons)} faces, "
                f"dimensions {tuple(round(v, 3) for v in obj.dimensions)}"
            )
            for line in mesh_report(obj):
                note(line)

        # Colours, from the materials, by value.
        colours = material_colours()
        note(f"materials: {len(bpy.data.materials)} in the file, {len(colours)} colour reads")
        seen: set = set()
        for object_name, material_name, route, rgba in colours:
            label = hue_label(rgba)
            key = (material_name, route, label)
            if key in seen:
                continue
            seen.add(key)
            note(
                f"colour {material_name} ({route}): "
                f"{[round(float(c), 3) for c in rgba[:3]]} -> {label}"
            )
        check(
            "some material reads red-ish",
            any(red_ish(rgba) for _, _, _, rgba in colours),
        )
        check(
            "and one reads yellow-ish",
            any(yellow_ish(rgba) for _, _, _, rgba in colours),
        )

        receipt = session.last_receipt
        check("the panel has a receipt for the turn", bool(receipt))
        if receipt:
            note(
                f"receipt: {receipt['title']!r} undoable={receipt['undoable']} "
                f"changed={receipt['changed']}"
            )

    def verify_undo() -> None:
        """One undo, and the whole turn is gone."""
        after = {obj.name for obj in bpy.data.objects}
        gained = sorted(after - before)
        note(f"before undo: {len(after)} objects, the turn added {gained}")
        try:
            result = bpy.ops.ed.undo()
        except Exception as exc:  # noqa: BLE001 - a refusal is a finding
            result = f"{type(exc).__name__}: {exc}"
        note(f"bpy.ops.ed.undo() -> {result}")
        bpy.context.view_layer.update()
        now = {obj.name for obj in bpy.data.objects}
        note(f"after undo: {len(now)} objects {sorted(now)}")
        check("one undo reverted the turn", not (set(gained) & now))
        check("and it left the scene as the turn found it", now == before)

    def verdict() -> None:
        dump_transcript(session)
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            note(f"could not write {LOG}: {exc}")
        note(f"screenshot: {SHOT}")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("LIVE OK" if not FAILURES else "LIVE FAILED", flush=True)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)

    def poll():
        if session.streaming:
            state["traces"] += 1
            if state["traces"] <= 8:
                note(
                    f"tick {state['traces']}: phase={session.phase} "
                    f"rows={len(session.messages)} "
                    f"drain_registered={bpy.app.timers.is_registered(bc.stream._tick)}"
                )
            # What the working strip says during a REAL turn, sampled rather than
            # photographed: a screenshot cannot show motion, and this is the record
            # of which phases a real model actually passes through. Also the reason
            # the claim "the strip is alive while a turn runs" is measured here and
            # not only with a scripted worker.
            if state["traces"] % 20 == 0:
                state["strip"].append(
                    f"{session.phase}/{session.busy_note() or '(none)'}"
                    f" {session.elapsed():.0f}s"
                )
                note(f"strip at {session.elapsed():.0f}s: {state['strip'][-1]}")
            if time.monotonic() - state["turn_started"] > TURN_DEADLINE:
                note(f"last status: {session.status!r}")
                FAILURES.append("the turn did not finish inside the deadline")
                verdict()
                return None
            return TICK

        step = state["step"]
        if step == 0:
            verify_turn()
            # Open the sidebar on this tick...
            window = bpy.context.window
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
            bc.stream.tag_view3d_redraw()
            state["step"] = 1
            return TICK

        if step == 1:
            # ...and select the tab on the next, because the assignment is only
            # accepted once the region has been laid out (measured, see
            # `screenshot`).
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
            # Frame the ball on this tick and photograph on the next, so the
            # picture is of the framed view rather than of the previous one.
            note(f"viewport: {frame_view(state['ball']) if state['ball'] else 'nothing to frame'}")
            state["step"] = 3
            return TICK

        if step == 3:
            # The picture is taken BEFORE the undo, so it shows the ball rather
            # than the empty scene the undo leaves.
            note(f"screenshot: {screenshot(SHOT)}")
            state["step"] = 4
            return TICK

        if step == 4:
            verify_undo()
            state["step"] = 5
            return TICK

        if step == 5:
            verdict()
            return None

        return None

    send_turn(SENTENCE)
    check("the turn opened", session.streaming)
    bpy.app.timers.register(poll, first_interval=TICK)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("LIVE FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
