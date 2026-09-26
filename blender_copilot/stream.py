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

import time

import bpy

from . import conversation, context, prompt, scope, toolbox, transport, undo_blender

# 50 ms. Ticket 11 measured launch-to-ready at 0.021 s and a JSON round trip at
# 0.02 ms, so this is two orders of magnitude above the IPC it is watching -
# which is the point: the tick must never be the bottleneck.
TICK = 0.05

# How often a tick repaints *purely to keep the working indicator moving*. The
# rule elsewhere in this file is that a redraw is tagged only when something the
# user can see changed - which, during a model's think, is never: a think produces
# no events at all. So the spinner would sit frozen in exactly the state it exists
# to announce, and a frozen spinner is indistinguishable from a dead one. Ten
# frames a second reads as motion and is a fraction of the repaints the event
# stream itself causes while text streams in.
ANIMATION_SECONDS = 0.1
_last_animation = 0.0

# See `set_budget_override`: a Compact press is a decision about this conversation, so
# it lives for the session rather than in the user's preferences. `None` means "use the
# preference, or the derived budget" - which is the state the add-on ships in.
_budget_override: int | None = None


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
    # Stamp what this request actually costs, before it goes: the provider's own token
    # count arrives later (its `usage` block) and the two are only useful together -
    # the pair is what turns the panel's bytes-per-token estimate into something a
    # reader can check against a measurement.
    conversation.session.request_bytes = context.request_bytes(messages, toolbox.SCHEMAS)
    return transport.worker.send(config, messages, toolbox.SCHEMAS)


def history_budget_bytes() -> int:
    """The projection's budget: a Compact override, else the preference, else derived.

    Read on every request rather than cached, so lowering the preference - or pressing
    Compact - shows up on the next round. A missing or unreadable preference is not an
    error: it means the derived default, because a turn must not fail over a budget.
    """
    if _budget_override is not None:
        return int(_budget_override)
    kib = 0
    try:
        addon = bpy.context.preferences.addons.get(__package__)
        kib = int(getattr(addon.preferences, "context_history_kib", 0) or 0)
    except Exception:  # noqa: BLE001 - no preferences, no window, or a stub context
        kib = 0
    if kib > 0:
        return context.budget_from_kib(kib)
    return context.DEFAULT_HISTORY_BUDGET_BYTES


def set_budget_override(bytes_: int | None) -> None:
    """Compact (or Restore, with `None`): a session-scoped budget for this conversation.

    Session-scoped on purpose. The ratified lever is the `context_history_kib`
    preference (8-512 KiB, ticket 14 §1) and it belongs to the user's *settings*; a
    button press is a decision about *this* conversation, so writing it to the
    preferences would leave it in place for every file opened afterwards. Nothing is
    written to `userpref.blend`, and reopening Blender restores the preference.
    """
    global _budget_override
    _budget_override = None if bytes_ is None else max(1, int(bytes_))


def budget_override() -> int | None:
    return _budget_override


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
    # The animation's own clock, and the one reason in this file to repaint when
    # nothing changed. Deliberately only while `streaming`: the indicator is drawn
    # then and only then, so a repaint for it in any other state is a repaint that
    # changes no pixel - and the tail of a cancelled turn (`worker.busy` with no
    # turn) would otherwise keep a still panel repainting until the child admitted
    # it.
    global _last_animation
    if conversation.session.streaming:
        moment = time.monotonic()
        if moment - _last_animation >= ANIMATION_SECONDS:
            _last_animation = moment
            changed = True
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
