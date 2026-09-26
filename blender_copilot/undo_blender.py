"""The half of the undo step that has to ask Blender.

`undo.py` holds the rule and imports no `bpy`; this holds the four things that
cannot be written without it - the before-image read, the
`depsgraph_update_post` flag, the one operator call, and the receipt handed to
the panel. Nothing here decides anything: every sentence the user sees and every
threshold comes from `undo.py`, and `pause_reason` is a two-line adapter over the
live context.

Three moments, each placed where it is for a reason:

  * `open_turn()` is called from `conversation.begin_turn`, through the injected
    `undo` seam, with the turn's own text. It takes the before-image there and
    not later, because the before-image has to describe the scene as it was
    before the agent touched anything.
  * `baseline()` runs immediately before a `run_blender_python` call, **once per
    session** - ticket 12 §1's baseline guard. It exists because one push with
    nothing behind it is not revertible: `ed.undo()` fails its own poll when the
    active step has no predecessor (measured, and it is what a session that has
    never pushed looks like).
  * `close_turn()` is called from `conversation._end_turn`, inside a `finally`.
    That is the single funnel every path out of a turn passes through - a normal
    reply, Stop, a cap, a failed transport, a raise in the model's own code - so
    the step exists on all of them, which is what ticket 12 §1 asks for. A
    tick-based observer could not make that claim: Stop ends the turn inside the
    operator that handled the click, and the next tick never sees the transition.
"""

from __future__ import annotations

import bpy

from . import conversation, execution, toolbox, undo

# The turn's record: the label for the step and the before-image to diff against.
# `None` means no turn is armed, which is also what makes the depsgraph handler
# free - it does nothing at all between turns.
_armed: dict | None = None

# The depsgraph handler's flag. The summary diff cannot see a change to an
# existing object's transform, so this is the signal that catches the commonest
# turn there is ("make the cube taller").
_touched = False

# The baseline guard. Session-scoped, never per turn (ticket 12 §1).
_baseline_done = False


# ---------------------------------------------------------------------------
# Reading the live state
# ---------------------------------------------------------------------------

def _summary(captured: str):
    """The bounded scene summary, or `None` when it cannot be read.

    Never raises: a turn must not fail, and an undo step must not be skipped,
    because a *read* misbehaved. `None` travels through `undo.changes` as "cannot
    tell", which produces no lines rather than an invented one.
    """
    try:
        return execution.scene_summary(toolbox.WORLD, captured=captured)
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _live() -> tuple[bool, str]:
    """`(use_global_undo, mode)` from the real context."""
    return (
        bool(bpy.context.preferences.edit.use_global_undo),
        str(getattr(bpy.context, "mode", "") or ""),
    )


def pause_reason(context=None) -> str | None:
    """What the panel should warn about now, or `None` (`undo.pause_reason`, §4).

    Takes the context the caller already has - the panel's `draw` gets one, and
    `Send` gets one - because reading `bpy.context` behind the caller's back is
    what makes this untestable off a GUI. `None` means "use the real one".

    A *warning*, not a gate: `Send` asks `blocking_reason`, so a reason returned
    here can be shown to the user without stopping them.
    """
    if context is None:
        global_undo, mode = _live()
    else:
        global_undo = bool(context.preferences.edit.use_global_undo)
        mode = str(getattr(context, "mode", "") or "")
    return undo.pause_reason(global_undo, mode)


def blocking_reason(context=None) -> str | None:
    """Why `Send` must refuse, or `None`.

    Strictly narrower than `pause_reason`: Global Undo off stops the turn, edit mode
    does not. That split is the owner's decision of 2026-09-26 - "let the user take
    the risk knowingly" - and it is the panel's gate and the `Send` operator's check,
    so the disabled button and the refusal cannot disagree with each other.
    """
    if context is None:
        global_undo, mode = _live()
    else:
        global_undo = bool(context.preferences.edit.use_global_undo)
        mode = str(getattr(context, "mode", "") or "")
    return undo.blocking_reason(global_undo, mode)


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------

def _push(label_text: str) -> str | None:
    """One `undo_push`, or the reason it did not happen.

    `{'CANCELLED'}` is treated as a failure rather than success: the operator
    returning anything but `FINISHED` means no step was recorded, and a receipt
    that claims an undoable turn in that state is the one lie this ticket must not
    tell.
    """
    try:
        result = bpy.ops.ed.undo_push(message=label_text)
    except Exception as exc:  # noqa: BLE001 - a refused push is a receipt, not a crash
        return f"{type(exc).__name__}: {exc}"
    if "FINISHED" not in result:
        return f"the operator returned {result!r}"
    return None


def open_turn(text: str) -> bool:
    """Open the turn's undo record, with the before-image. True when it opened one.

    Called from `conversation.begin_turn`. Idempotent, so a second open for one
    turn cannot move the before-image forward past the mutation it is supposed to
    precede.
    """
    global _armed, _touched
    if _armed is not None:
        return False
    _touched = False
    _armed = {
        "text": text,
        "label": undo.label(text),
        "before": _summary("turn_start"),
    }
    return True


def forget() -> bool:
    """Drop an armed turn without pushing anything. True when there was one.

    Exactly one caller, and it is the case that would otherwise be wrong: a file
    switch (`scope.switch`) abandons the turn that belonged to the file being
    left, and a push judged after that would record the *new* file's state under
    the old turn's label. Undo cannot follow a file load anyway - the stack is
    discarded by it - so forgetting is the honest answer rather than a lost step.
    """
    global _armed, _touched
    had_turn = _armed is not None
    _armed = None
    _touched = False
    return had_turn


def baseline(call) -> bool:
    """Push the marker behind the turn's step, before the first code call.

    Ticket 12 §1's baseline guard, and its scope is the *session*: the marker is
    taken once, before the agent's first `run_blender_python`, so the step the
    turn-end push creates always has a predecessor for Ctrl+Z to land on. A marker
    is not a receipt - it changes nothing the panel shows - and it is deliberately
    not taken for the two reader tools, which cannot mutate.

    Marked attempted even when the push fails: a session where every code call
    retried a failing push would be noisier than one missing a baseline, and the
    turn-end receipt already says when Blender refused a push.
    """
    global _baseline_done
    if _baseline_done or _armed is None:
        return False
    if conversation.call_name(call) != execution.RUN_PYTHON:
        return False
    _baseline_done = True
    return _push(undo.BASELINE_LABEL) is None


def close_turn() -> bool:
    """The turn is over: one step, and the panel's receipt. True when it ran.

    The order below is load-bearing and not a style choice - see the comment on
    the after-image read.
    """
    global _armed, _touched
    turn = _armed
    _armed = None
    if turn is None:
        return False
    touched = _touched
    _touched = False

    # The after-image FIRST. `depsgraph_update_post` fires when the depsgraph is
    # next *evaluated*, not when the property is written: measured 2026-09-26 on
    # the installed 5.2.2, a bare `obj.scale.z = 1.5` produced 0 events and the
    # `view_layer.update()` after it produced 1 (and a read-only update produced
    # none). Reading the summary is what evaluates it, so checking the flag before
    # this read would miss exactly the change this ticket exists to catch - a
    # scale on an existing object, which the bounded summary cannot see.
    after = _summary("turn_end")
    # Ticket 12 §3's trigger: the flag, *or* a non-empty summary diff. Either one
    # alone is insufficient - the flag cannot name what changed, and the diff
    # cannot see a transform.
    mutated = touched or bool(undo.changes(turn["before"], after))

    pushed, reason, detail = False, "", ""
    if mutated:
        reason = pause_reason() or ""
        if not reason:
            detail = _push(turn["label"]) or ""
            if detail:
                reason = undo.REFUSED_PUSH_FAILED
            else:
                pushed = True

    conversation.session.last_receipt = (
        undo.receipt(
            turn["before"],
            after,
            pushed=pushed,
            reason=reason,
            detail=detail,
            label_text=turn["label"],
        )
        if mutated
        else None
    )
    return True


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _on_depsgraph_update(_scene, _depsgraph) -> None:
    """A depsgraph evaluation happened while a turn was armed: something moved.

    `persistent`, like `scope.py`'s handlers and for the same measured reason: a
    handler without the decorator is removed on a file load, so it would stop
    firing after the user's first Open and the flag would go quiet for the rest of
    the session.

    Deliberately blunt - any evaluation, whatever caused it. A false positive here
    costs one extra undo step (the safe direction: pushing when something changed
    is never the failure mode); a false negative costs the user their only way
    back. Reads do not trip it: `view_layer.update()` on an unchanged scene fired
    0 events in the same measurement.
    """
    global _touched
    if _armed is not None:
        _touched = True


def start() -> None:
    handlers = bpy.app.handlers.depsgraph_update_post
    if _on_depsgraph_update not in handlers:
        handlers.append(_on_depsgraph_update)


def stop() -> None:
    handlers = bpy.app.handlers.depsgraph_update_post
    if _on_depsgraph_update in handlers:
        handlers.remove(_on_depsgraph_update)
