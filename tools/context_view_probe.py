#!/usr/bin/env python3
"""Photograph the context viewer, collapsed and expanded, and measure the difference.

    python3 tools/bounded_run.py 150 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/context_view_probe.py

No provider, no credential, no money: this is a GUI probe. `SMOKE OK` in
`tools/panel_draw_smoke.py` already proves the block *draws*; what it cannot prove is
that a human sees anything, because a stub layout counts widgets and not pixels.

What this measures, in pixels, from the sidebar region's own screenshot:

  * the number of text rows the panel draws with the viewer collapsed, and with it
    expanded, and that the second is larger - the causal claim that the toggle does
    what it says. The states differ by exactly one flag, so a picture of some *other*
    panel (Item, Tool, ...) would show a delta of zero and fail loudly. That is the
    discriminating check `tools/spacing_probe.py` had to learn the hard way, when it
    photographed Blender's Transform panel and its log claimed otherwise.
  * how many rows of the block are actually on screen at the default sidebar height,
    which is a fact about this panel's placement that no stub can see: the block sits
    below an unbounded transcript, and an unbounded transcript can push it off the
    fold. The number is reported rather than asserted, because "it fits" depends on
    how tall the user's sidebar is.

It writes `logs/context-view.txt` and the two PNGs, prints `CONTEXT OK` /
`CONTEXT FAILED`, and quits Blender itself, so the bounded driver's deadline stays a
backstop. `AGENTS.md`: Blender exits 0 even when a `--python` script raises, so
**grep for the token**, never the status.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bpy  # noqa: E402

# The band reader, the sidebar lookup and the area screenshot are the proven ones -
# written for the spacing probe, where a version that counted single bright pixels as
# text reported 34 phantom rows. Importing them is the point: a second copy would be
# a second set of bugs. `spacing_probe` guards its `main()`, so importing runs only
# its definitions.
import spacing_probe as sp  # noqa: E402

EXT = "bl_ext.user_default.blender_copilot"
LOG = ROOT / "logs" / "context-view.txt"
SHOT_COLLAPSED = ROOT / "logs" / "context-view-collapsed.png"
SHOT_EXPANDED = ROOT / "logs" / "context-view-expanded.png"
# The panel's text column, as an inset from the sidebar's own edges in the screenshot.
# `screenshot_area` writes the whole *area*, not the region (measured: the picture is
# 1580 px wide while the region is 335), so the column has to be located from the
# region's geometry rather than guessed - a first version scanned x 45..230, which is
# the viewport's own labels, and found four rows of text in a panel full of them.
TEXT_PAD = 20

NOTES: list[str] = []
FAILURES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(f"t={time.monotonic() - STARTED:6.2f}s  {line}")
    print(f"   {line}", flush=True)


def check(label: str, condition: bool, extra="") -> bool:
    ok = bool(condition)
    note(f"{'ok  ' if ok else 'FAIL'} {label}{'' if ok else f'  <- {extra}'}")
    if not ok:
        FAILURES.append(label)
    return ok


def enable() -> str:
    try:
        bpy.ops.preferences.addon_enable(module=EXT)
    except Exception as exc:  # noqa: BLE001 - reported, and the probe still runs
        return f"addon_enable: {type(exc).__name__}: {exc}"
    return "enabled"


def seed(conversation) -> None:
    """History for the projection, and an EMPTY transcript for the picture.

    Both halves matter, and the second was learned from a failed run. The projection
    reads `history`, so that is where the categories get their bytes - the mix is
    deliberately the acceptance turn's: a prompt, a model reply, a code call and its
    result. The *transcript* stays empty because `_draw_boxes` renders each exchange
    as a boxed paragraph: five seeded messages made the panel taller than the 775 px
    sidebar, which pushed the record and layout blocks below the fold in BOTH states
    and made the pictures measure the fold instead of the viewer.
    """
    session = conversation.session
    session.messages.clear()
    session.history = [
        {"role": "user", "content": "Generate me a Football of Red and yellow colors "
                                    "instead of black and white"},
        {"role": "assistant", "content": "I will build an icosphere and split the "
                                         "pentagons from the hexagons.",
         "tool_calls": [{"id": "c1", "type": "function", "function": {
             "name": "run_blender_python",
             "arguments": '{"purpose": "build the ball", "code": "x" * 400}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": '{"ok": true, "stdout": "80 vertices, 42 faces"}'},
        {"role": "user", "content": "now make the pentagons red and the hexagons yellow"},
        {"role": "assistant", "content": "Done - 12 red pentagons and 30 yellow hexagons."},
    ]


def main() -> int:
    # The *registered* add-on, not a second copy of the package: importing
    # `blender_copilot` by path would give a different module object with its own
    # session and its own `_budget_override`, and the panel would be drawing from the
    # one this probe never touched. That is exactly what the first run of this probe
    # did, and it reported "no addon preferences to drive".
    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        note("FAILED: no preferences - the extension is not registered as an add-on")
        print("CONTEXT FAILED", flush=True)
        return 1
    panel_module = bc.panel
    conversation = bc.conversation
    stream = bc.stream
    settings = addon.preferences

    # The viewer is collapsed by default and expanded for the second photograph; the
    # preference is restored at the end so the probe does not change what the user
    # sees next time they open the sidebar.
    original_expanded = bool(settings.show_context)
    settings.show_context = False

    seed(conversation)
    state = {"step": 0, "attempts": 0, "bands": {}}

    def poll():
        step = state["step"]
        if step == 0:
            parts = sp.sidebar()
            if parts is None:
                FAILURES.append("no sidebar to open")
                return verdict()
            area, region = parts
            area.spaces.active.show_region_ui = True
            region.tag_redraw()
            state["step"] = 1
            return 0.15
        if step == 1:
            parts = sp.sidebar()
            if parts is None:
                FAILURES.append("no sidebar to select")
                return verdict()
            area, region = parts
            try:
                region.active_panel_category = "Copilot"
            except Exception as exc:  # noqa: BLE001 - reported
                note(f"could not select 'Copilot': {exc}")
            region.tag_redraw()
            state["attempts"] += 1
            if state["attempts"] < 4:
                return 0.15
            note(f"region {region.width}x{region.height}, category "
                 f"{region.active_panel_category!r}")
            state["step"] = 2
            return 0.3
        if step == 2:
            # What the block is showing, from the projection itself. This is the
            # cross-check between the picture and the arithmetic: the report is what
            # the draw reads, so a screenshot that disagrees with it means the draw
            # is not using the report.
            report = panel_module.context_report()
            note("viewer report: " + ", ".join(
                f"{name} {panel_module._size(size)}"
                for name, size in report["sizes"].items()
            ))
            note(f"total {panel_module._size(report['total'])} "
                 f"\u2248{panel_module._count(report['tokens'])} tokens "
                 f"({report['fraction'] * 100:.2f}% of the window)")
            note(f"model sees {report['sent_messages']}/{report['history_messages']} "
                 f"messages, budget {panel_module._size(report['cached_bytes'])} "
                 f"(override {stream.budget_override()})")
            _redraw()
            state["step"] = 3
            return 0.4
        if step == 3:
            note("collapsed: " + sp.screenshot(SHOT_COLLAPSED))
            _redraw()
            state["step"] = 4
            return 0.4
        if step == 4:
            rows, widest = read_bands(SHOT_COLLAPSED)
            state["bands"]["collapsed"] = rows
            note(f"collapsed: {len(rows)} text rows, widest {widest} bright px")
            settings.show_context = True
            _redraw()
            state["step"] = 5
            return 0.4
        if step == 5:
            note("expanded: " + sp.screenshot(SHOT_EXPANDED))
            _redraw()
            state["step"] = 6
            return 0.4
        if step == 6:
            rows, widest = read_bands(SHOT_EXPANDED)
            state["bands"]["expanded"] = rows
            note(f"expanded: {len(rows)} text rows, widest {widest} bright px")
            return verdict()
        return None

    def read_bands(path: Path):
        """Text rows inside the sidebar's column of the area screenshot.

        The x range comes from the region, not from a constant: the screenshot is of
        the whole 3D Viewport and the sidebar is its rightmost `region.width` pixels.
        """
        parts = sp.sidebar()
        region = parts[1] if parts else None
        width = 0
        try:
            image = bpy.data.images.load(str(path))
            width = int(image.size[0])
            bpy.data.images.remove(image)
        except Exception as exc:  # noqa: BLE001 - reported through the band count
            note(f"could not size {path.name}: {exc}")
        if not width or region is None:
            return sp.bands(path, 0, 10_000)
        x0 = max(0, width - int(region.width) + TEXT_PAD)
        x1 = max(x0 + 1, width - TEXT_PAD)
        note(f"measuring x {x0}..{x1} of {width} px (region {region.width})")
        return sp.bands(path, x0, x1)

    def _redraw() -> None:
        """Repaint the sidebar through the same path the add-on uses.

        `region.tag_redraw()`, never `Area.tag_redraw()`: the area call does not
        repaint a sidebar, which is how an earlier probe photographed the wrong
        panel while its log said it had not.
        """
        parts = sp.sidebar()
        if parts is None:
            return
        parts[1].tag_redraw()
        try:
            with bpy.context.temp_override(window=bpy.context.window, area=parts[0]):
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        except Exception:  # noqa: BLE001 - a redraw hint, not a guarantee
            pass

    def verdict():
        collapsed = state["bands"].get("collapsed", [])
        expanded = state["bands"].get("expanded", [])
        delta = len(expanded) - len(collapsed)
        note(f"rows drawn: collapsed {len(collapsed)}, expanded {len(expanded)}, "
             f"delta {delta}")
        check("the sidebar was photographed with the Copilot tab selected",
              len(collapsed) >= 3, collapsed)
        check("the collapsed viewer draws its heading, its header pair and a bar",
              len(collapsed) >= 3, len(collapsed))
        # The causal claim. A picture of a different panel cannot move by flipping a
        # flag only this panel reads, so a delta of zero is a failed photograph and
        # not a small delta.
        check("expanding the viewer draws more rows than collapsing it", delta > 0, delta)
        check("and the extra rows are the block's own, not one stray pixel row",
              6 <= delta <= 40, delta)
        settings.show_context = original_expanded
        _redraw()
        try:
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            note(f"could not write {LOG}: {exc}")
        note(f"pictures: {SHOT_COLLAPSED.name} (collapsed), {SHOT_EXPANDED.name} (expanded)")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("CONTEXT OK" if not FAILURES else "CONTEXT FAILED", flush=True)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(1 if FAILURES else 0)
        return None

    bpy.app.timers.register(poll, first_interval=0.5)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("CONTEXT FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
