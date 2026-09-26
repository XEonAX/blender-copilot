"""The drain, the loop's clock, and the repaint it drives.

This is the mechanism the whole design leans on: a `bpy.app.timers` callback is
the only thing that may touch `bpy` from the "other side", and it is also the
only place a worker event becomes visible state. Each tick does **exactly one**
of three things, in this order of preference (ticket 09 §0):

  1. **drain** - `transport.worker.tick()` is non-blocking; it hands over whatever
     the reader threads queued, plus the transport's own time-based verdicts (no
     `ready` inside the timeout, cancel grace elapsed, the child exited). Each
     event goes through `session.apply_event`, which reports whether anything the
     user can see has changed.
  2. **execute one tool call** - `session.pump()`, and only on a tick that drained
     nothing. A tool call blocks the main thread for its whole duration, so the
     tick that queued its `running…` row must not be the tick that runs it, or the
     row would never be painted.
  3. **finalize** - `pump()` again, on the tick that finds the queue empty and
     sends the next round or stops for a cap.

Then `region.tag_redraw()`, once, and only if something changed: tagging on every
tick would repaint continuously and burn CPU for nothing. Return `None` while
idle, so Blender unregisters the timer and an idle panel costs nothing.

Three rules from ticket 11 live in the shape of this file: the main thread
never waits for the child, never joins a thread, and never blocks on a read.
Each of those would freeze Blender's UI for the length of an HTTP request.

This module is also where the loop's collaborators are wired, because they are
the three things that need `bpy` and `conversation.py` may not have it: the
sender, the tool executor, and the live scene summary. `attach` is the same seam
the CPython checks use with fakes.

The tick is also where the undo side's baseline marker goes in, immediately
before a `run_blender_python` call (`undo_blender.baseline`) - the one part of the
undo rule that has to run inside the loop's tick rather than at a turn's edge,
because it has to happen *before* the code it protects. The edge itself (one step
and one receipt per turn) is wired through `conversation.attach` and lives in
`conversation._end_turn`.

Deliberately a module-level function, not a bound method: timers holding bound
methods have historically needed a keep-alive workaround, and a plain function
sidesteps it.
"""

from __future__ import annotations

import bpy

from . import conversation, context, prompt, scope, toolbox, transport, undo_blender

# 50 ms. Ticket 11 measured launch-to-ready at 0.021 s and a JSON round trip at
# 0.02 ms, so this is two orders of magnitude above the IPC it is watching -
# which is the point: the tick must never be the bottleneck.
TICK = 0.05


def _prefs():
    """The add-on's preferences, or `None`.

    Read here rather than passed in because the loop's sender is a module-level
    callback with no context: `conversation.attach` takes one argument and the
    whole point of that seam is that the bpy-free module never sees a context.
    Never raises - a background check, a stub context or a missing add-on means
    "no preferences", which leaves `transport.config` on its environment route.
    """
    try:
        addon = bpy.context.preferences.addons.get(__package__)
    except Exception:  # noqa: BLE001 - no window, no preferences, no add-on
        return None
    return addon.preferences if addon else None


def _send_round(messages: list[dict]) -> str | None:
    """One request for the loop, over the worker's pipe.

    A function rather than a lambda because the loop calls it on every round after
    the first: the config is re-read each time, and the tool schemas ride along
    every round, since a round that does not declare them is a round the model
    cannot call anything from.

    The preferences go in so that a round two of a turn started with an in-panel
    key keeps working, and so that a key the user fixes mid-turn is picked up on
    the next round rather than at the next Send.
    """
    config = transport.config(_prefs())
    if config.problem:
        return config.problem
    return transport.worker.send(config, messages, toolbox.SCHEMAS)


def history_budget_bytes() -> int:
    """The projection's budget: the addon's preference, or the derived default.

    Ticket 14 §1's `context_history_kib` (8-512 KiB) when the user set one - a
    lever for a smaller model than the shipped backend - and otherwise the budget
    derived from the provider's own documented window (`context.derive_budget_bytes`),
    which is the floor the design was written against finally replaced by the real
    figure. 0 means "derive", and is what ships.

    Read on every request rather than cached, so lowering the preference shows up
    on the next round. A missing or unreadable preference is not an error: it
    means the derived default, because a turn must not fail over a budget.
    """
    kib = 0
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        kib = int(getattr(addon.preferences, "context_history_kib", 0) or 0)
    except Exception:  # noqa: BLE001 - no preferences, no window, or a stub context
        kib = 0
    if kib > 0:
        return context.budget_from_kib(kib)
    return context.DEFAULT_HISTORY_BUDGET_BYTES


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
    """One bounded step, and never two (ticket 09 §0).

    The steps are *drain*, *execute one tool call*, and *finalize* - and a tick
    does exactly one of them. That is not tidiness: the tick that drains a reply
    carrying `tool_calls` is the tick that queues the rows, so executing a call in
    the same callback would run it before the panel had ever drawn its `running…`
    row. One step per tick is what makes that row real rather than theoretical.
    """
    changed = False
    was_streaming = conversation.session.streaming
    try:
        events = transport.worker.tick()
        if events:
            for event in events:
                if conversation.session.apply_event(event):
                    changed = True
        else:
            # A code call is about to run: put the baseline marker behind it
            # first, if this session has never pushed one (ticket 12 §1). It has
            # to happen *before* the call, because its whole purpose is to record
            # the scene as it was before the agent touched it.
            pending = conversation.session.next_call
            if pending is not None:
                undo_blender.baseline(pending)
            # Only when nothing was drained: a tool call blocks the main thread
            # for its whole duration, so it gets a tick to itself.
            if conversation.session.pump():
                changed = True
    finally:
        # Ticket 04's write point: a turn that just ended is the one moment the
        # conversation can hold anything new. The undo step for that same turn is
        # written by `conversation._end_turn` instead, so that Stop, a cap and a
        # failed transport all get one too - they end a turn without this tick
        # ever seeing the transition.
        if was_streaming and not conversation.session.streaming and scope.persist():
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


# Wire the loop once, at import. `conversation.session` is the process's single
# conversation (ticket 18: a second panel is an identical view of it), so the
# collaborators belong to the module that can see `bpy`, not to the turn. `undo`
# rides the same seam: the loop tells it when a turn opens and closes, and never
# learns what a `undo_push` is.
conversation.session.attach(
    send=_send_round,
    execute=toolbox.execute,
    context=prompt.context,
    undo=undo_blender,
    budget=history_budget_bytes,
)
