"""Measure the sidebar region the panel actually lives in.

    python3 tools/bounded_run.py 60 -- env BC_QUIT=1 \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --factory-startup --python tools/panel_width_probe.py

A Panel's `draw()` cannot run at startup, so this reads the *region* geometry
instead: `context.region.width` inside draw is the same number as the `UI` region
width of the 3D Viewport's sidebar.

Why this exists: the addon wrapped prose at a fixed character count, which is
wrong by construction because the sidebar is user-resizable. Making it responsive
needs to know how many pixels a character occupies at the UI's current scale, and
that cannot be guessed - this measures the region so the constant is calibrated
against real geometry rather than invented.

Quits Blender itself, so the bounded driver's deadline is a backstop and not the
mechanism.
"""

import json
import os
import sys

import bpy

OUT = "/tmp/panel_width_probe.json"


def main() -> None:
    found = []
    windows = bpy.context.window_manager.windows
    for window in windows:
        entry = {
            "window_px": [window.width, window.height],
            "screen": window.screen.name,
            "areas": [],
        }
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            regions = {r.type: [r.width, r.height] for r in area.regions}
            entry["areas"].append({"area_px": [area.width, area.height], "regions": regions})
        found.append(entry)

    # The character width is what the wrap budget actually needs. Measure it with
    # blf rather than estimating it from a screenshot: the addon's draw code runs
    # with Blender's UI font already selected, so `blf.dimensions` there returns
    # the same numbers this does for a given size.
    import blf

    fonts = []
    for size in (11, 12, 13):
        blf.size(0, size)
        sample = "n" * 64
        total = blf.dimensions(0, sample)[0]
        fonts.append({"size": size, "px_per_char": round(total / 64, 3)})

    report = {
        "blender": bpy.app.version_string,
        "ui_scale": getattr(bpy.context.preferences.system, "ui_scale", None),
        "window_px_scale": bpy.context.preferences.system.pixel_size,
        "ui_font_size_pref": bpy.context.preferences.ui_styles[0].widget.points,
        "blf_px_per_char": fonts,
        "view3d_areas": found,
    }
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print("WIDTH_PROBE | " + json.dumps(report)[:600], flush=True)
    print(f"WIDTH_PROBE | wrote {OUT}", flush=True)

    if os.environ.get("BC_QUIT"):
        bpy.ops.wm.quit_blender()


main()
