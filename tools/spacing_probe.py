#!/usr/bin/env python3
"""What vertical pitch does a column of text get, and can it be tightened?

    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --python tools/spacing_probe.py

The question a panel cannot answer about itself. The prose in a reply is drawn as
one label per wrapped line - the panel has no `label_multiline`, which is
5.3.0-alpha only (the installed 5.2.2's RNA exposes no such function; measured, and
it is why this probe exists rather than that call) - and each label is a full UI
row, so a paragraph is as tall as its line count times a *row* rather than times a
*line*.

So this registers a throwaway panel beside the real one, draws the same six short
lines four ways, and measures the result **from the pixels**: the screenshot is
loaded back, each row's bright-pixel count inside the panel's text column is
scanned, and a "text line" is a measured run of rows. The report is a pitch in
pixels per variant, which is the only form of the answer that cannot be argued with.

Variants:

  A  six separate labels - what the panel does today, the baseline;
  B  `scale_y = 0.75` on the column - if a label row's height is what sets the
     pitch, this should shorten it by a quarter;
  C  one label whose text holds newlines - whether a plain label honours them at
     all, given that only 5.3's `label_multiline` grows a row to fit them;
  D  six labels in a column with `align=True`, which Blender's own source sets the
     inter-item space to zero for (`flow->space_ = flow->align() ? 0 : columnspace`).

Two traps this probe fell into, both recorded because a measurement that lies is
worse than none:

  * `Area.tag_redraw()` does **not** repaint the sidebar region. The first two runs
     photographed the *Item* panel - the tab selected before - while the log
     reported "selected category: 'Copilot Spacing'", and the pixel scan then
     measured Blender's own Transform rows. The region is tagged here, twice, and
     the screenshot is taken a tick later.
  * a check that only counts bands proves nothing about *which* panel was drawn.
     The lines are a fixed, known count, so the count itself is the check.

Prints `SPACING OK`, writes `logs/spacing.png` and `logs/spacing.txt`, and quits
Blender itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parent.parent
SHOT = ROOT / "logs" / "spacing.png"
LOG = ROOT / "logs" / "spacing.txt"
CATEGORY = "Copilot Spacing"

LINE = "Structure found, six curves, 26 keys each."
BLOCK = (LINE, LINE, LINE, LINE)
# A gap wider than this separates one variant's block from the next in the measured
# picture: the panel draws `separator(factor=2.0)` between them, which is wider than
# any measured line pitch (the largest is 31 px) and narrower than the separator.
BLOCK_GAP = 36
# A, B and D each draw a header plus four lines; C draws a header plus one label,
# however many lines that label turns out to render. So this floor is a fact about
# the panel that was drawn: fewer lines than this means the picture is of another
# tab, whatever the log says - which is exactly the mistake the first two runs made.
MIN_LINES = 17

NOTES: list[str] = []
FAILURES: list[str] = []


def note(line: str) -> None:
    NOTES.append(line)
    print(f"SPACING | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


class SPACING_PT_probe(bpy.types.Panel):
    """A throwaway panel: one paragraph, drawn four ways, and nothing else."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Line spacing"

    def draw(self, context):
        layout = self.layout

        # A wide gap between the blocks, so the measurement can tell where one ends
        # and the next begins without reading the text - which is what makes each
        # variant's pitch a measurement rather than an inference from a mixed list.
        def gap():
            layout.separator(factor=3.0)

        layout.label(text="A: separate labels (today)")
        for line in BLOCK:
            layout.label(text=line)
        gap()

        layout.label(text="B: scale_y 0.75")
        quarter = layout.column()
        quarter.scale_y = 0.75
        for line in BLOCK:
            quarter.label(text=line)
        gap()

        layout.label(text="C: one label with newlines")
        layout.label(text="\n".join(BLOCK))
        gap()

        layout.label(text="D: align=True and scale_y 0.75")
        both = layout.column(align=True)
        both.scale_y = 0.75
        for line in BLOCK:
            both.label(text=line)


def bands(path: Path, x0: int, x1: int, threshold: float = 0.6, min_pixels: int = 4):
    """Rows containing text, as [(first_row, last_row)], read from the picture.

    `image.pixels` is bottom-up RGBA; `foreach_get` into a numpy buffer is the only
    way to scan a screenshot without a Python loop per pixel, and Blender bundles
    numpy. A row counts as text when at least `min_pixels` of its pixels inside the
    panel's text column are brighter than `threshold`: the panel's text is near-white
    on dark grey, so brightness is the text/background test - but a *count* rather
    than a maximum, because a single bright pixel (a box border, an icon edge, the
    category tab strip) otherwise marks a whole row as text. That mistake is why the
    first run of this probe reported 34 "text bands", several of them one pixel tall.
    """
    import numpy as np

    image = bpy.data.images.load(str(path))
    try:
        width, height = image.size
        buffer = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buffer)
        grid = buffer.reshape(height, width, 4)
        column = grid[:, max(0, x0) : min(width, x1), :3]
        counted = (column.max(axis=2) > threshold).sum(axis=1)

        found = []
        start = None
        for row in range(height):
            bright = bool(counted[row] >= min_pixels)
            if bright and start is None:
                start = row
            elif not bright and start is not None:
                found.append((start, row - 1))
                start = None
        if start is not None:
            found.append((start, height - 1))
        return found, int(counted.max())
    finally:
        bpy.data.images.remove(image)


def sidebar():
    """`(area, region)` for the 3D Viewport's sidebar, or `None`."""
    window = getattr(bpy.context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return None
    area = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        return None
    region = next((r for r in area.regions if r.type == "UI"), None)
    if region is None:
        return None
    return area, region


def screenshot(path: Path) -> str:
    parts = sidebar()
    if parts is None:
        return "no sidebar to photograph"
    area, region = parts
    try:
        with bpy.context.temp_override(window=bpy.context.window, area=area, region=region):
            result = bpy.ops.screen.screenshot_area(filepath=str(path))
        return f"{result} -> {path.name}"
    except Exception as exc:  # noqa: BLE001 - reported
        return f"failed: {type(exc).__name__}: {exc}"


def main() -> int:
    try:
        bpy.utils.register_class(SPACING_PT_probe)
    except Exception as exc:  # noqa: BLE001 - reported
        note(f"FAILED: could not register the probe panel: {exc}")
        return 1

    state = {"step": 0}

    def verdict() -> None:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - reported
            note(f"could not write {LOG}: {exc}")
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("SPACING OK" if not FAILURES else "SPACING FAILED", flush=True)
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            sys.exit(0 if not FAILURES else 1)

    def measure() -> None:
        parts = sidebar()
        if parts is None:
            FAILURES.append("no sidebar to measure")
            return
        area, region = parts
        # The panel's own text column: in from the left inset, and clear of the
        # category tabs on the region's right edge, which are text too and would
        # otherwise add a band per tab.
        left = region.x - area.x + 14
        right = region.x - area.x + region.width - 48
        note(
            f"measuring columns {left}..{right} of the {region.width}px sidebar "
            f"(category {region.active_panel_category!r})"
        )
        found, widest = bands(SHOT, left, right)
        note(f"most text pixels in one row: {widest}")
        lines = [(top, bottom) for top, bottom in found if bottom - top + 1 >= 4]
        thin = [(top, bottom) for top, bottom in found if bottom - top + 1 < 4]
        note(f"{len(found)} bands, {len(lines)} of them >=4px tall; thin ones: {thin}")

        # Image rows run bottom-up, so bands arrive bottom-of-panel first: the
        # last-drawn block (D) is scanned first. Blocks are the runs between gaps
        # wider than BLOCK_GAP, which is why the panel draws a wide separator between
        # them - the alternative is guessing which band belongs to which variant.
        previous = None
        gaps: list[int] = []
        for index, (top, bottom) in enumerate(lines):
            pitch = "" if previous is None else f"  gap {top - previous:>3} px"
            if previous is not None:
                gaps.append(top - previous)
            note(f"line {index:>2}: rows {top:>4}..{bottom:<4} height {bottom - top + 1:>2}{pitch}")
            previous = top

        groups: list[list[int]] = [[]]
        for gap in gaps:
            if gap > BLOCK_GAP:
                groups.append([])
            groups[-1].append(gap)

        # The check that makes the rest of this worth reading, and it comes *after*
        # the gaps are known because that is what discriminates: a count of lines is
        # not enough, since Blender's own Transform panel has 17-odd rows too and
        # slipped past a count-only version of this check in the probe's own gate run.
        # This panel's tightest lines are 19-21 px apart and no stock panel draws rows
        # that close, so the three closest gaps are the signature.
        tightest = sorted(gaps)[:3]
        note(f"the three closest gaps: {tightest}")
        check(
            f"the picture is of this panel (want >={MIN_LINES} lines and tight rows)",
            len(lines) >= MIN_LINES and len(tightest) == 3 and max(tightest) < 24,
        )
        names = [
            "D: align=True + scale_y 0.75",
            "C: newlines",
            "B: scale_y 0.75",
            "A: separate labels",
        ]
        note("blocks in scan order (bottom of the panel first): " + ", ".join(names))
        pitches: dict[str, int] = {}
        for index, group in enumerate(groups):
            label = names[index] if index < len(names) else f"block {index}"
            pitch = sorted(group)[len(group) // 2] if group else 0
            pitches[label] = pitch
            note(
                f"block {label}: {len(group) + 1} measured lines, gaps {group} "
                f"-> pitch {pitch or 'n/a'} px"
            )
        # The comparison the whole probe exists for. Written as a check rather than a
        # paragraph so a future edit that quietly undoes it fails here.
        baseline = pitches.get("A: separate labels", 0)
        packed = pitches.get("D: align=True + scale_y 0.75", 0)
        if baseline and packed:
            check(
                f"an aligned column is tighter than separate labels "
                f"({packed} px vs {baseline} px)",
                packed < baseline,
            )
            note(
                f"the same paragraph is {(1 - packed / baseline) * 100:.0f}% shorter "
                f"in an aligned column"
            )
        else:
            note("not all four blocks were measured, so there is nothing to compare")

    def poll():
        step = state["step"]
        if step == 0:
            parts = sidebar()
            if parts is None:
                FAILURES.append("no sidebar to open")
                verdict()
                return None
            area, region = parts
            area.spaces.active.show_region_ui = True
            region.tag_redraw()
            state["step"] = 1
            return 0.1
        if step == 1:
            # The tab is set on its own tick - `active_panel_category` is read-only
            # until the region has been laid out (measured, and it bit this repo
            # once) - and it is set on *every* tick from here to the screenshot,
            # because the assignment can be accepted by the RNA while the drawn
            # region still shows the previous tab. That is not hypothetical: this
            # probe's gate run photographed the Item panel while its log reported
            # "selected category: 'Copilot Spacing'".
            parts = sidebar()
            if parts is None:
                FAILURES.append("no sidebar to select")
                verdict()
                return None
            area, region = parts
            try:
                region.active_panel_category = CATEGORY
            except Exception as exc:  # noqa: BLE001 - reported
                note(f"could not select {CATEGORY!r}: {exc}")
            # The *region*, not the area: `Area.tag_redraw()` does not repaint the
            # sidebar.
            region.tag_redraw()
            state["attempts"] = state.get("attempts", 0) + 1
            note(
                f"attempt {state['attempts']}: category "
                f"{region.active_panel_category!r}"
            )
            if state["attempts"] < 4:
                return 0.12
            state["step"] = 2
            return 0.2
        if step == 2:
            # One more redraw on its own tick, so the frame that exists is the frame
            # that gets photographed.
            parts = sidebar()
            if parts is not None:
                parts[1].tag_redraw()
            state["step"] = 3
            return 0.15
        if step == 3:
            note(f"screenshot: {screenshot(SHOT)}")
            state["step"] = 4
            return 0.2
        if step == 4:
            measure()
            state["step"] = 5
            verdict()
            return None
        return None

    bpy.app.timers.register(poll, first_interval=0.1)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("SPACING FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
