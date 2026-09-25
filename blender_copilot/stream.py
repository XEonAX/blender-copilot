"""The repaint pump.

This is the mechanism the whole design leans on, so it is worth proving early:
a `bpy.app.timers` callback is the only thing that may touch `bpy` from the
"other side", and it drives the panel's repaint by tagging the region.

Two rules worth keeping when this becomes real:

  * Only tag a redraw when something actually changed. `tag_redraw()` on every
    tick would repaint continuously and burn CPU for nothing.
  * Return `None` to have Blender unregister the timer. The timer only exists
    while there is something to show, so an idle panel costs nothing.

Deliberately a module-level function, not a bound method: timers holding bound
methods have historically needed a keep-alive workaround, and a plain function
sidesteps it.
"""

from __future__ import annotations

import bpy

from . import conversation

TICK = 0.1


def tag_view3d_redraw() -> int:
    """Tag every visible 3D Viewport sidebar for repaint. Returns how many."""
    tagged = 0
    try:
        window_manager = bpy.context.window_manager
    except Exception:
        # No window (background mode, or a timer fired during shutdown).
        # Timers never pump under `blender -b` anyway.
        return 0

    for window in window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "UI":
                    region.tag_redraw()
                    tagged += 1
    return tagged


def _tick():
    if not conversation.session.advance():
        return None
    tag_view3d_redraw()
    return TICK if conversation.session.streaming else None


def start() -> None:
    if conversation.session.streaming and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)


def stop() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
