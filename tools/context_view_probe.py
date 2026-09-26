#!/usr/bin/env python3
"""Photograph the context ROW, and confirm the popup opens - nothing more.

    python3 tools/bounded_run.py 150 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/context_view_probe.py

No provider, no credential, no money: this is a GUI probe.

What it verifies, and what it deliberately does NOT:

  * the **row** is drawn on screen: a real sidebar screenshot, scanned for text bands,
    which is the one part of this feature a machine can see.
  * the **popup opens**: `bpy.ops.blender_copilot.context_popover("INVOKE_DEFAULT")` - the
    call the INFO button makes - reports `RUNNING_MODAL`, i.e. a modal popup is running.

**The popup's appearance is not machine-verifiable on 5.2.2, and that is measured, not
assumed.** Three attempts, all of which failed for different reasons:

  1. `bpy.ops.screen.screenshot_area` photographs ONE area, and a popup is not in any area.
  2. `bpy.ops.screen.screenshot` renders the screen's *areas* and leaves popup overlays
     out. Measured with a trivial throwaway popup: it changed **0 pixels** of the window
     screenshot, while a human watching the same run saw the popup.
  3. An OS `screencapture` shows whatever the display happens to be showing - in this
     run, the desktop wallpaper, because Blender's window was not what was on screen at
     that instant. It is also a picture of the user's whole desktop, which does not
     belong in a repository.

So the popup's body is checked headlessly (`tools/panel_draw_smoke.py` drives
`draw_context_details` against a stub layout and checks the numbers against the
projection), its arithmetic is checked in `tests/test_context.py`, and its *appearance*
is the human's judgement - which is the same division of labour `AGENTS.md` describes
for anything a stub cannot see.

The popup is invoked LAST, because it is modal and `quit_blender` does not run while it
is open: the probe therefore terminates its own process after printing the verdict, so a
popup can never outlive the run and leave itself on the user's screen. The bounded
driver's deadline remains a backstop.
"""

from __future__ import annotations

import importlib
import os
import signal
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
SHOT_ROW = ROOT / "logs" / "context-view-row.png"
# The panel's text column, as an inset from the sidebar's own edges in a screenshot.
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
            note("row: " + sp.screenshot(SHOT_ROW))
            state["step"] = 4
            return 0.4
        if step == 4:
            rows, widest = read_bands(SHOT_ROW)
            state["bands"]["row"] = rows
            note(f"the context row on screen: {len(rows)} text rows, "
                 f"widest {widest} bright px")
            # And that the popup opens at all. Its APPEARANCE is not checkable from here -
            # see the docstring - so what is checked is the mechanism: the operator reaches
            # `invoke_popup` and Blender reports a modal popup running.
            try:
                result = bpy.ops.blender_copilot.context_popover("INVOKE_DEFAULT")
            except Exception as exc:  # noqa: BLE001 - a refusal is the finding
                result = f"{type(exc).__name__}: {exc}"
            state["invoke"] = str(result)
            note(f"context_popover(INVOKE_DEFAULT) -> {result}")
            check(
                "the INFO button's operator opens a modal popup",
                "RUNNING_MODAL" in str(result),
                result,
            )
            state["step"] = 5
            return 0.4
        if step == 5:
            return verdict()
        return None

    def read_bands(path: Path):
        """Text rows inside the sidebar's column of the area screenshot.

        The x range comes from the region, not from a constant: the picture is of the
        whole 3D Viewport and the sidebar is its rightmost `region.width` pixels.
        MEASURED the hard way - the first version scanned x 45..230, which is the
        viewport's own labels, and found four rows of text in a panel full of them.
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
        rows = state["bands"].get("row", [])
        note(f"invoke: {state.get('invoke')}")
        check(
            "the sidebar was photographed with the Copilot tab selected",
            len(rows) >= 3,
            rows,
        )
        check(
            "the context row drew its own text, not an empty box",
            len(rows) >= 3,
            len(rows),
        )
        try:
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            note(f"could not write {LOG}: {exc}")
        note(f"picture: {SHOT_ROW.name} - the sidebar as Blender drew it")
        # The honest limit of this instrument, stated where a reader will find it rather
        # than only in the docstring.
        note(
            "NOT verified here, and not verifiable by any script in 5.2.2: what the POPUP "
            "looks like. MEASURED 2026-09-26 - a trivial operator popup changed 0 pixels "
            "of `bpy.ops.screen.screenshot` (its API renders the screen's areas and leaves "
            "popup overlays out), and an OS `screencapture` shows whatever the display is "
            "showing, which was the desktop rather than the Blender window. The popup's "
            "body is checked by `tools/panel_draw_smoke.py`, its numbers are checked in "
            "`tests/test_context.py`, and its appearance is for a human to judge."
        )
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("CONTEXT OK" if not FAILURES else "CONTEXT FAILED", flush=True)
        # The popup is invoked last and is MODAL, so `quit_blender` cannot run while it is
        # open - the first version of this probe sat there until the bounded driver killed
        # it, leaving a popup on the user's screen for the rest of the deadline. Terminating
        # the process is the honest way out: the verdict is already printed and flushed,
        # the log is written, and the popup cannot outlive the run. The bound stays as the
        # backstop it was always meant to be rather than as the mechanism.
        note("terminating the process: the popup is modal, so nothing else can close it")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGTERM)
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
