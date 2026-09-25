"""The drain, and the repaint it drives.

This is the mechanism the whole design leans on: a `bpy.app.timers` callback is
the only thing that may touch `bpy` from the "other side", and it is also the
only place a worker event becomes visible state. Each tick:

  1. `transport.worker.tick()` - non-blocking. It drains whatever the reader
     threads queued and applies the transport's own time-based verdicts (no
     `ready` inside the timeout, cancel grace elapsed, the child exited).
  2. every event goes through `session.apply_event`, which reports whether
     anything the user can see has changed.
  3. `region.tag_redraw()`, once, and only if something changed. Tagging on
     every tick would repaint continuously and burn CPU for nothing.
  4. Return `None` while idle, so Blender unregisters the timer and an idle
     panel costs nothing.

Three rules from ticket 11 live in the shape of this file: the main thread
never waits for the child, never joins a thread, and never blocks on a read.
Each of those would freeze Blender's UI for the length of an HTTP request.

Deliberately a module-level function, not a bound method: timers holding bound
methods have historically needed a keep-alive workaround, and a plain function
sidesteps it.
"""

from __future__ import annotations

import bpy

from . import conversation, transport

# 50 ms. Ticket 11 measured launch-to-ready at 0.021 s and a JSON round trip at
# 0.02 ms, so this is two orders of magnitude above the IPC it is watching -
# which is the point: the tick must never be the bottleneck.
TICK = 0.05


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
    changed = False
    for event in transport.worker.tick():
        if conversation.session.apply_event(event):
            changed = True

    if changed:
        tag_view3d_redraw()

    # Keep pumping while either side still has work. `busy` is what covers the
    # gap between the UI showing "stopped" and the child admitting it; without
    # it, a cancelled-but-unacknowledged turn would never be drained.
    if conversation.session.streaming or transport.worker.busy:
        return TICK
    return None


def start() -> None:
    if (
        conversation.session.streaming or transport.worker.busy
    ) and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)


def stop() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
