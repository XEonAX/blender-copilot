"""One undo step per turn, and the receipt that says what it holds.

This module imports no `bpy`. The whole rule - the label, the mutation diff, the
exact sentences and the decision to pause - is decided here, where
`tests/test_conversation.py` can disagree with it. `undo_blender.py` is the half
that has to ask Blender, and it does nothing but supply this module's inputs and
make the one operator call.

The discipline is *What replaces undo as the recovery mechanism?* §1, transcribed
rather than re-decided:

  * **The unit is the user's turn**, never the tool call. After "make this
    thicker" ran four tool calls, one Ctrl+Z must revert all four: the user
    reasons in requests, and a half-applied turn (object created, material
    assignment lost) is worse than none.
  * **One push at the end, in a `finally`.** Pushing at the *start* reverts too
    far - measured in *Confirm the four undo cases in a GUI* case 5, the negative
    control: cube at `z=0`, push, `z=5.0`, one undo, and **the cube is gone**
    rather than back at 0. So the change it was meant to protect was not merely
    unprotected but unreachable.
  * **Only if the turn mutated.** An empty step makes Ctrl+Z look broken.
  * **Refused in edit mode, not attempted.** `ED_undo_is_memfile_compatible`
    declines while the active object is in an edit mode
    (`source/blender/editors/undo/ed_undo.cc:588`), so a push there returns
    `{'FINISHED'}` and records nothing usable; the undo after it **removes the
    object being edited** (case 5 above, and re-measured headlessly by
    `tools/undo_step_probe.py`).

And §3 for the receipt, whose honesty limit is the important part: the diff is
over the **bounded `get_scene_info` summary** - the same object the model is sent
as its live scene summary - so it sees datablock-level change and never
sub-object data. A scale on an existing cube is invisible to it, and the receipt
therefore says *that the scene's data changed*, never "exactly this property
changed". A property-level RNA crawl was rejected as unbounded on a large scene.

`pause_reason` is §4, amended by the owner on 2026-09-26: with Global Undo off
auto-run is **paused** - there is no recovery at all for the one class the addon
can recover, and the fix is one click in the banner. In edit mode it is **not**
paused any more. The measurement behind the old pause still holds (`undo_push`
there records nothing usable and the undo after it deletes the object), so the
push is still refused - but refusing to *start* meant the agent was unusable in
the mode where modelling happens, blocking even a question. The owner's decision
was that a user may take the risk **knowingly**: `PAUSE_LINES` states it in the
panel before they send, and `blocking_reason` is what the panel and `Send` gate on.
"""

from __future__ import annotations

# --- the step's label (ticket 12 §1) ------------------------------------------

LABEL_PREFIX = "copilot: "
LABEL_CHARS = 40

# The baseline marker's label. `undo_push` behind the turn's own step is what one
# Ctrl+Z lands on; without it a session that has never pushed has nothing behind
# the push and `ed.undo()` fails its own poll (measured).
BASELINE_LABEL = "copilot: before the first change"

AGENT_TURN = "agent turn"

# --- the diff (ticket 12 §3) --------------------------------------------------

# The honest generic line, for a change the bounded summary cannot see. Ticket 12
# §3: the receipt says "objects/data changed", never "exactly these properties
# changed".
UNNAMED_CHANGE = "the scene's data changed"

# How many change lines the panel draws. A receipt that outgrows a sidebar is
# worse than one that says "and 3 more" - the count is what the user needs, and
# the transcript above already holds the detail.
RECEIPT_MAX_LINES = 4

# --- refusals and pauses ------------------------------------------------------

REFUSED_EDIT_MODE = "edit_mode"
REFUSED_GLOBAL_UNDO = "global_undo_off"
REFUSED_PUSH_FAILED = "push_failed"

PAUSE_GLOBAL_UNDO = "global_undo_off"
PAUSE_EDIT_MODE = "edit_mode"

# The banner under the header, per pause (ticket 12 §4 and §5's degraded-mode
# sentence). Each is a tuple of sentences rather than one string because the panel
# has no wrapping control and draws one label per line.
PAUSE_LINES = {
    PAUSE_GLOBAL_UNDO: (
        "Global Undo is off.",
        "Nothing the agent runs can be undone.",
        "Auto-run is paused.",
    ),
    PAUSE_EDIT_MODE: (
        "Blender is in edit mode.",
        "No undo step is recorded for this turn: in edit mode",
        "a step records nothing usable, and the undo after",
        "one deletes the object being edited.",
        "Ctrl+Z will then undo this turn together with your own",
        "last change, rather than this turn alone.",
        "Press Tab first if you want this turn to have its own step.",
    ),
}

# The one-click fix the banner offers, per pause. Edit mode's fix is the user's
# own Tab key: the addon does not leave edit mode for them, because that is a
# visible change to their session (and the exit is not ours to take).
PAUSE_ACTION = {
    PAUSE_GLOBAL_UNDO: "blender_copilot.enable_global_undo",
    PAUSE_EDIT_MODE: "",
}

# §5's sentences, verbatim in content. The coverage sentence is the *persistent*
# one and rides on every receipt, so the promise cannot grow with the turn.
REVERT_LINE = "Ctrl+Z reverts this turn."
HISTORY_LINE = "Ctrl+Alt+Z opens Blender's undo history."
COVERAGE_LINE = (
    "Undo covers local scene data only: not files, network, preferences or "
    "Python state."
)

# The detectable outside-the-blend effect (§5, last bullet): a Save As, or the
# first save of an unsaved file, is visible as a `filepath`/`is_saved` change and
# is named rather than generally warned about. An overwrite save is *not*
# reliably distinguishable, and network and subprocess effects are not detectable
# at all - which is why the coverage sentence, not this line, carries the
# non-coverage.
OUTSIDE_SAVE_LINE = (
    "This turn saved or renamed the .blend - undo does not cover files."
)

REFUSAL_LINES = {
    REFUSED_EDIT_MODE: (
        "Blender was in edit mode when the turn ended.",
        "No undo step was recorded: in edit mode a step records nothing usable,",
        "and the undo after one deletes the object.",
        # MEASURED 2026-09-26, `tools/undo_step_probe.py` case 10, with a step of the
        # user's own pushed between the baseline and the turn: scale 1.0 at the
        # baseline, 2.0 in the user's own step, 3.0 set by the turn, and one Ctrl+Z in
        # edit mode landed on 1.0 - the turn AND the user's own change. Nothing was
        # deleted (the object list was unchanged), which is the part the push refusal
        # buys, so the sentence names what is actually lost rather than the old guess.
        "Ctrl+Z undoes this turn together with your own last change.",
    ),
    REFUSED_GLOBAL_UNDO: (
        "Global Undo is off, so nothing this turn ran can be undone.",
    ),
    REFUSED_PUSH_FAILED: (
        "Blender would not record an undo step for this turn.",
    ),
}


# ---------------------------------------------------------------------------
# The step's label
# ---------------------------------------------------------------------------

def label(user_text: str) -> str:
    """`copilot: <the first ~40 characters of the user's turn>` (ticket 12 §1).

    One line, cut at a word: the label is what the user reads in Blender's own
    Undo History to find the step, and a label that ends mid-word is one they have
    to decode. The marker is appended inside the budget rather than after it, so
    the total stays inside `BKE_UNDO_STR_MAX` (64 in the installed build).
    """
    collapsed = " ".join((user_text or "").split())
    if not collapsed:
        return LABEL_PREFIX + AGENT_TURN
    if len(collapsed) > LABEL_CHARS:
        cut = collapsed[:LABEL_CHARS]
        space = cut.rfind(" ")
        # Only honour the word boundary when it does not throw away most of the
        # budget - a turn whose first word is 60 characters long still gets a
        # label rather than a single word.
        if space >= LABEL_CHARS // 2:
            cut = cut[:space]
        collapsed = cut.rstrip() + "\u2026"
    return LABEL_PREFIX + collapsed


# ---------------------------------------------------------------------------
# What the turn changed
# ---------------------------------------------------------------------------

def _show(value) -> str:
    """One value, as the receipt spells it."""
    if value is None:
        return "none"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_show(item) for item in value) or "none"
    return str(value)


def _arrow(before, after) -> str:
    return f"{_show(before)} \u2192 {_show(after)}"


def _type_lines(before: dict, after: dict) -> list[str]:
    """Per-type object counts, sorted so the same change always reads the same."""
    old = before or {}
    new = after or {}
    out: list[str] = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            out.append(f"{key}: {_arrow(old.get(key), new.get(key))}")
    return out


def _object_lines(before: dict, after: dict) -> list[str]:
    return [f"objects: {_arrow(before.get('object_count'), after.get('object_count'))}"]


def _active_lines(before: dict, after: dict) -> list[str]:
    old = _show((before.get("active") or {}).get("name"))
    new = _show((after.get("active") or {}).get("name"))
    if old == new:
        return []
    return [f"active: {old} \u2192 {new}"]


def _selection_lines(before: dict, after: dict) -> list[str]:
    old = (before.get("selection") or {}).get("count")
    new = (after.get("selection") or {}).get("count")
    if old == new:
        return []
    return [f"selection: {_arrow(old, new)}"]


def _tree_lines(before: dict, after: dict) -> list[str]:
    """Collections, named without pretending to be precise.

    The bounded tree carries names and per-collection counts, so a diff of it can
    double-report what `objects:` already said. "collections changed" is the
    honest sentence; the count difference is above it.
    """
    if before.get("collection_tree") == after.get("collection_tree") and (
        before.get("collection_tree_truncated") == after.get("collection_tree_truncated")
    ):
        return []
    return ["collections changed"]


# Ordered: what the user is most likely to have asked for first, then the rest.
#
# Three groups of keys are deliberately absent, and each for its own reason:
#
#   * `captured` and `schema` describe the *capture*, not the scene, and `captured`
#     differs between the two reads by construction - comparing it would report a
#     change that did not happen.
#   * `is_dirty` flips on every mutation, so it would appear on every receipt and
#     say nothing: a scale on the existing cube would come back as
#     "unsaved changes: no → yes", which reads like a save and is not one.
#   * `is_saved`'s only interesting transition *is* a save, and `outside_effects`
#     owns that sentence.
#
# `filepath` stays: a Save As is worth naming in both places.
_PLAIN_FIELDS = (
    ("mode", "mode"),
    ("scene", "scene"),
    ("frame", "frame"),
    ("frame_range", "frame range"),
    ("render_engine", "engine"),
    ("unit_system", "units"),
    ("global_undo", "Global Undo"),
    ("filepath", "file"),
)


def changes(before, after) -> list[str]:
    """The receipt's lines: what changed between two bounded summaries.

    `before` and `after` are the `get_scene_info` summary objects - the same
    fields the model is sent - or `None`, which means "could not be read" and
    produces nothing rather than a guess. The result is *also* the push trigger
    (ticket 12 §3): a non-empty diff is one of the two signals that the turn
    touched the scene, the other being the depsgraph flag, which was measured to
    catch a change this cannot see.
    """
    if not before or not after:
        return []
    lines: list[str] = []
    if before.get("object_count") != after.get("object_count"):
        lines += _object_lines(before, after)
    if before.get("object_type_counts") != after.get("object_type_counts"):
        lines += _type_lines(
            before.get("object_type_counts") or {}, after.get("object_type_counts") or {}
        )
    lines += _active_lines(before, after)
    lines += _selection_lines(before, after)
    lines += _tree_lines(before, after)
    for key, name in _PLAIN_FIELDS:
        if before.get(key) != after.get(key):
            lines.append(f"{name}: {_arrow(before.get(key), after.get(key))}")
    return lines


def outside_effects(before, after) -> list[str]:
    """Effects a turn had *outside* the `.blend`, when they are detectable.

    Ticket 12 §5: name the effect instead of warning generally. A save is the one
    that is visible at all, and it is visible only as the file path or the
    never-saved flag moving.
    """
    if not before or not after:
        return []
    if before.get("filepath") != after.get("filepath"):
        return [OUTSIDE_SAVE_LINE]
    if bool(before.get("is_saved")) is False and after.get("is_saved"):
        return [OUTSIDE_SAVE_LINE]
    return []


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

def _capped(lines: list[str]) -> tuple[list[str], int]:
    if len(lines) <= RECEIPT_MAX_LINES:
        return list(lines), 0
    return list(lines[:RECEIPT_MAX_LINES]), len(lines) - RECEIPT_MAX_LINES


def receipt(
    before,
    after,
    *,
    pushed: bool,
    reason: str = "",
    detail: str = "",
    label_text: str = "",
) -> dict:
    """What the panel shows after a turn that changed something.

    The shape is small on purpose - `title`, `changed`, `lines` are what the panel
    draws; `undoable`, `label`, `reason` are what the answer and the probe cite -
    and every sentence in `lines` comes from a constant above, so no path can
    invent a softer promise than §5's.
    """
    named = changes(before, after)
    if pushed and not named:
        # The depsgraph flag fired and the bounded summary saw nothing: a scale on
        # an existing object is exactly this case. Say that much, honestly, rather
        # than showing an empty receipt.
        named = [UNNAMED_CHANGE]
    drawn, dropped = _capped(named)

    lines: list[str] = []
    if pushed:
        lines.append(REVERT_LINE)
    else:
        lines.extend(REFUSAL_LINES.get(reason, ()))
        if detail:
            lines.append(f"Blender said: {detail}")
    lines.extend(outside_effects(before, after))
    lines.append(COVERAGE_LINE)
    if pushed:
        lines.append(HISTORY_LINE)

    return {
        "undoable": bool(pushed),
        "title": "Undoable" if pushed else "Not undoable",
        "label": label_text,
        "reason": reason,
        "changed": drawn,
        "more": dropped,
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# When auto-run must pause
# ---------------------------------------------------------------------------

def pause_reason(global_undo: bool, mode: str) -> str | None:
    """What to warn the user about, or `None` (ticket 12 §4, amended 2026-09-26).

    Two inputs, both readable from `bpy.context` and neither needing a probe:
    the preference (measured valid: with it off, `bpy.ops.ed.undo()` raises
    `RuntimeError: Operator bpy.ops.ed.undo.poll() failed`) and the mode
    (`ED_undo_is_memfile_compatible` declines for `OB_MODE_EDIT`).

    **`global_undo` is the only usable detector.** Ticket 12's addendum claimed a
    second, mechanical one - "attempting the push fails, so the addon need not
    read the preference". Measured 2026-09-26 on the installed 5.2.2: with Global
    Undo off, `bpy.ops.ed.undo_push()` returns `{'FINISHED'}` and only the
    *undo* raises. A push is therefore not a detector, and this function is why
    the addon reads the preference instead of hoping.

    Edit mode is the other input, and it is stricter than §4 could know: the push
    *succeeds* there and the next undo deletes the object being edited. That is why
    the push is still refused in edit mode - but it is a warning rather than a gate,
    because the owner decided on 2026-09-26 that a user may take that risk knowingly,
    having read what it costs. `blocking_reason` is the gate.
    """
    if not global_undo:
        return PAUSE_GLOBAL_UNDO
    if str(mode or "").startswith("EDIT_"):
        return PAUSE_EDIT_MODE
    return None


def blocking_reason(global_undo: bool, mode: str) -> str | None:
    """Why `Send` must refuse, or `None` - the gate, as opposed to the warning.

    Global Undo off is the whole of it: the preference is the one input that makes
    *every* turn unrecoverable, and the banner offers its one-click fix. Edit mode is
    deliberately absent (owner, 2026-09-26): the turn runs, the push is refused there
    by `close_turn`, and `PAUSE_LINES` tells the user what they are accepting.

    A separate function rather than a flag on `pause_reason`, so a caller cannot
    accidentally treat "warn about this" as "stop".
    """
    return PAUSE_GLOBAL_UNDO if not global_undo else None
