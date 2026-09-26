"""Run every layout variant's draw body without a GUI.

    /Applications/Blender.app/Contents/MacOS/Blender --background \
        --factory-startup --python tools/panel_draw_smoke.py

A Panel's `draw()` cannot run in background mode - there is no window context -
but its *body* is ordinary Python over a `UILayout`. This substitutes a stub
layout that records the calls, so the branching (which variant draws what, for
every message kind, expanded and collapsed) is exercised. It proves the code
runs; it proves nothing about how it looks. Visual judgement is the human's.

`layout.textbox` is recorded, not tested: it needs a real region.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bpy  # noqa: E402
import blender_copilot as bc  # noqa: E402
from blender_copilot import (  # noqa: E402
    budget,
    context as context_module,
    conversation,
    execution,
    panel,
    prompt,
    scope,
    toolbox,
    undo,
)


class StubLayout:
    def __init__(self, log):
        self._log = log
        self.alert = False
        self._enabled = True
        self.alignment = ""

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        # Recorded, because "Send is disabled while auto-run is paused" is a
        # claim about a drawn property and nothing else in this file can see it.
        self._enabled = bool(value)
        if not value:
            self._log.append(("disabled", "", ""))

    def _child(self):
        return StubLayout(self._log)

    def row(self, align=False):
        self._log.append("row")
        return self._child()

    def box(self):
        self._log.append("box")
        return self._child()

    def column(self, align=False):
        return self._child()

    def split(self, factor=0.5, align=False):
        return self._child()

    def label(self, text="", **kwargs):
        self._log.append(("label", text, kwargs.get("icon", "")))

    def operator(self, idname, **kwargs):
        self._log.append(("operator", idname, kwargs.get("text", "")))
        return self._child()

    def prop(self, data, name, **kwargs):
        self._log.append(("prop", name, ""))

    def progress(self, factor=0.0, text="", type="BAR", **kwargs):
        # The `type` argument is what distinguishes the two busy primitives: RING
        # is the working strip's arc and BAR is the shine along the prompt editor,
        # and a check that only counted "a progress bar was drawn" would pass with
        # the wrong one in the wrong place.
        self._log.append(("progress", type, f"{factor:.2f}"))
        return self._child()

    def textbox(self, data, name, **kwargs):
        self._log.append(("textbox", name, ""))

    def separator(self, factor=1.0):
        pass

    def template_list(self, *args, **kwargs):
        self._log.append(("template_list", args[0] if args else "", ""))


# A label that does not fit is MIDDLE-CLIPPED by Blender, not wrapped. Three
# clipping bugs reached a human's screen before any check existed, so this counts
# characters. Two data points from the owner's screenshots set the limit:
#   * 104 chars in a box  -> CLIPPED ("Request failed: ...othing changed.")
#   *  65 chars in a box  -> renders whole (the Ctrl+Alt+Z receipt line)
# So 70 catches gross overflow without reporting things that are known to fit.
#
# STATED LIMITATION, because a check that overstates itself is worse than none:
# this CANNOT catch the first two bugs, which were ~55-character labels in a BARE
# COLUMN, where the usable width is much smaller than in a box. Fitting depends on
# the sidebar's pixel width and the user's font, neither of which the stub knows.
# Treat a line here as a real bug and its absence as no evidence at all.
LABEL_SOFT_LIMIT = 70


def long_labels(log: list) -> list:
    """(length, kind, text) for every drawn label past the soft limit."""
    out = []
    for entry in log:
        if isinstance(entry, tuple) and len(entry) == 3:
            kind, text = entry[0], entry[1]
            if kind in ("label", "operator") and text and len(text) > LABEL_SOFT_LIMIT:
                out.append((len(text), kind, text))
    return sorted(out, reverse=True)


def send_is_disabled(log: list) -> bool:
    """Whether the Send row itself was drawn disabled.

    The flag alone is not specific: the running-tool note disables a row too, and
    the demo transcript always has a running tool. What identifies the Send row is
    a disable marker on the very entry before the Send operator - the panel
    assigns `enabled` and then draws the button in the same sub-row.
    """
    for index, entry in enumerate(log):
        if entry != ("disabled", "", "") or index + 1 >= len(log):
            continue
        following = log[index + 1]
        if (
            isinstance(following, tuple)
            and following[0] == "operator"
            and following[1] == "blender_copilot.send"
        ):
            return True
    return False


def longest_label(log: list) -> int:
    """The longest label actually drawn.

    Reported unconditionally, not derived from the over-limit list: the first
    version of this took its value from that list, so it printed 0 whenever
    nothing exceeded the limit - a check that says nothing while looking like it
    says something. The running maximum is the useful number, because it shows
    text creeping toward the edge before any single string crosses it.
    """
    return max(
        (
            len(entry[1])
            for entry in log
            if isinstance(entry, tuple)
            and len(entry) == 3
            and entry[0] in ("label", "operator")
            and entry[1]
        ),
        default=0,
    )


def fake_context(use_global_undo=True, mode="OBJECT"):
    """A context with the two things the pause rule reads.

    `mode` is settable because the edit-mode pause is a second reason Send is
    refused, and only a stub can put the panel in it without a GUI.
    """
    return SimpleNamespace(
        preferences=SimpleNamespace(edit=SimpleNamespace(use_global_undo=use_global_undo)),
        mode=mode,
        window=None,
    )


def main() -> None:
    bc.register()
    try:
        # A Panel RNA type cannot be instantiated off the GUI, so bind its
        # plain-Python draw methods to a stand-in self.
        instance = SimpleNamespace()
        for name, member in vars(panel.BLENDER_COPILOT_PT_panel).items():
            if name.startswith("_draw"):
                setattr(instance, name, member.__get__(instance))
        context = fake_context()
        settings = SimpleNamespace(layout_variant="boxes", prompt_text="", newest_first=True)

        # The live session starts EMPTY now that Send runs a real turn, so the
        # layout fixture is seeded here instead of by `register()`. One
        # conversation holding every kind the panel can render, which is what
        # these draw bodies exist to exercise.
        conversation.session.messages.clear()
        conversation.session.messages.extend(conversation._demo())
        # The receipt is built by the **real rule** against a real change to this
        # run's scene, not typed in here. A hand-written fixture is how this file
        # would keep passing after `undo.receipt` changed shape - which is what
        # happened when ticket 05 replaced the two-key receipt, and it failed
        # loudly, which is the point of building it this way instead.
        before = execution.scene_summary(toolbox.WORLD, captured="turn_start")
        bpy.context.scene.collection.objects.link(bpy.data.objects.new("Target", None))
        after = execution.scene_summary(toolbox.WORLD, captured="turn_end")
        conversation.session.last_receipt = undo.receipt(
            before, after, pushed=True, label_text="copilot: add a target empty"
        )
        assert conversation.session.last_receipt["changed"], conversation.session.last_receipt

        # Every variant, both reading orders, both expander states. The order is in
        # here because it is the one thing in the panel that a reader notices
        # immediately and the stub *can* check: it records labels in the order the
        # draw bodies emit them, which is exactly the order Blender would paint.
        #
        # The anchors are the three NEWEST prompts, newest first, rather than the
        # whole transcript's ends, and each is short enough to survive wrapping -
        # a whole sentence is split across two labels at this measure, so an anchor
        # containing one would only ever match in the variant that does not wrap.
        # The flat-log variant also keeps only the newest `VISIBLE_LINES`, so an
        # assertion pinned to the oldest prompt would be an assertion about a line
        # that variant deliberately dropped. Every anchor that *is* drawn must still
        # be in the order asked for, and at least two of the three always are.
        ANCHORS = (
            "Now give it a red material",
            "Make the cube taller.",
            "What is selected right now?",
        )
        for variant in ("log", "boxes", "external"):
            for newest_first in (True, False):
                for expanded in (False, True):
                    for message in conversation.session.messages:
                        message.expanded = expanded
                    settings.newest_first = newest_first
                    log: list = []
                    layout = StubLayout(log)
                    # The stub context has no `region`, so wrap_budget falls back to
                    # the fixed measure - which is the point: this proves the draw
                    # bodies RUN, not that they fit. Fitting is checked by
                    # long_labels/longest_label below and, ultimately, by an eye.
                    wrap = panel.wrap_budget(context)
                    if variant == "log":
                        instance._draw_log(layout, context, newest_first)
                    elif variant == "external":
                        instance._draw_external(layout, context)
                    else:
                        instance._draw_boxes(layout, context, wrap, newest_first)
                    instance._draw_header(layout, context)
                    instance._draw_working(layout, context)
                    instance._draw_transport(layout, wrap)
                    instance._draw_receipt(layout, wrap)
                    instance._draw_input(layout, context, settings)
                    instance._draw_actions(layout, context, wrap)
                    instance._draw_coverage(layout, wrap)
                    instance._draw_variant_picker(layout, settings)
                    worst = long_labels(log)
                    longest = longest_label(log)
                    print(
                        f"ok   variant={variant:8s} newest_first={newest_first!s:5s} "
                        f"expanded={expanded!s:5s} widgets={len(log)} "
                        f"boxes={log.count('box')} longest_label={longest}"
                    )
                    # Advisorily and deliberately blunt: only gross overflow is
                    # caught, and the bare-column case above slips past it entirely.
                    # Absence of a line here is NOT evidence that the text fits.
                    for length, kind, text in worst[:4]:
                        print(
                            f"     !! {length} chars will likely be clipped "
                            f"({kind}): {text!r}"
                        )

                    if variant == "external":
                        continue
                    drawn = [entry[1] for entry in log if entry[0] == "label"]
                    if variant == "log":
                        # The flat-log variant is a faithful renderer of one list, so
                        # that is what is checked: exactly the lines it was handed, in
                        # the order it was handed them. Which end of that list is
                        # newest is `visible_lines`' decision, and the CPython suite
                        # checks it there - without a GUI, where the truncation is
                        # explicit. Anchoring on prompt text here instead would fail
                        # whenever the truncation (or an expanded traceback) happened
                        # to drop the anchor, and a check that quietly skips is worse
                        # than one that checks nothing.
                        lines = conversation.session.visible_lines(
                            newest_first=newest_first
                        )
                        assert drawn[: len(lines)] == lines, (
                            newest_first,
                            drawn[:4],
                            lines[:4],
                        )
                        continue

                    def at(needle):
                        return next(
                            (i for i, text in enumerate(drawn) if needle in text), None
                        )

                    found = [(needle, at(needle)) for needle in ANCHORS]
                    found = [(needle, index) for needle, index in found if index is not None]
                    assert len(found) >= 2, (variant, drawn)
                    order = [index for _, index in found]
                    if newest_first:
                        assert order == sorted(order), (variant, found)
                    else:
                        assert order == sorted(order, reverse=True), (variant, found)
        print(
            "ok   newest-first puts the live turn first, and chronological order is "
            "still available"
        )

        # The working strip and the shine: nothing is drawn for either when idle,
        # and both appear while a turn is in flight. Both halves, because a block
        # that only ever draws is as broken as one that never does.
        log = []
        instance._draw_working(StubLayout(log), context)
        assert log == [], log
        log = []
        instance._draw_input(StubLayout(log), context, settings)
        assert not [e for e in log if e[0] == "progress"], log
        print("ok   no ring and no shine while idle")

        conversation.session.begin_turn("look busy for a moment")
        try:
            log = []
            instance._draw_working(StubLayout(log), context)
            rings = [e for e in log if e[0] == "progress"]
            assert rings and rings[0][1] == "RING", log
            assert ("label", conversation.session.busy_note(), "") in log, log
            log = []
            instance._draw_input(StubLayout(log), context, settings)
            bars = [e for e in log if e[0] == "progress"]
            assert bars and bars[0][1] == "BAR", log
            assert ("textbox", "prompt_text", "") in log, log
        finally:
            conversation.session.cancel()
        print(
            "ok   the ring and the shine are drawn while a turn is in flight, and "
            "the prompt editor is still there"
        )

        # The transport failure block, which draws nothing when there is no
        # failure - both halves, because a block that only ever draws is as
        # broken as one that never does.
        log = []
        instance._draw_transport(StubLayout(log), panel.wrap_budget(context))
        assert log == [], log
        conversation.session.set_transport_error("DEEPSEEK_API_KEY not set")
        log = []
        instance._draw_transport(StubLayout(log), panel.wrap_budget(context))
        assert (
            "operator",
            "blender_copilot.retry_transport",
            "Retry",
        ) in log, log
        print("ok   the transport failure block draws, and only when it should")
        conversation.session.set_transport_error(None)

        # A failed API call and a running tool must both be reachable by drawing.
        # With the pager gone this is ONE pass over the whole transcript, which
        # makes the check stronger than it was: nothing can hide on a page nobody
        # turned to. The icons are the stub's record of what the panel said about
        # each state - ERROR and TIME are its own vocabulary for the two.
        log = []
        instance._draw_boxes(StubLayout(log), context, panel.wrap_budget(context), True)
        icons = {entry[2] for entry in log if entry[0] == "label"}
        assert "ERROR" in icons, log
        assert "TIME" in icons, log
        print("ok   the whole transcript draws: error block and running tool included")

        # Stop replaces Send only while a turn is in flight.
        conversation.session.begin_turn("check the stop slot")
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        assert ("operator", "blender_copilot.stop", "Stop") in log, log
        conversation.session.cancel()
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        assert ("operator", "blender_copilot.send", "Send") in log, log
        print("ok   Stop replaces Send only while a turn is in flight")

        # --- the budget's copy on screen (build ticket 07) --------------------
        # The running-call note is the *whole* contract, because it is drawn
        # before the call starts and the panel cannot be repainted while the call
        # runs: what stops, what only stops once it returns, that Stop cannot be
        # delivered, and what to do when the code does not stop at all. Ticket 17
        # measured each of those, and every one of them is a claim about pixels.
        #
        # The one copy lives in `budget.py`; this checks that the panel draws it
        # rather than a second, hand-typed version of it.
        conversation.session.begin_turn("show the running note")
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert all(line in joined for line in budget.RUNNING_LINES), joined
        assert f"{budget.CALL_SECONDS:.0f}s per call" in joined, joined
        assert f"{budget.TURN_SECONDS:.0f}s of code per turn" in joined, joined
        assert "stops at the budget" in joined, joined
        assert "only stops once it returns" in joined, joined
        assert "cannot be delivered" in joined, joined
        assert "swallows the interrupt" in joined, joined
        assert "force-quit" in joined, joined
        assert longest_label(log) <= LABEL_SOFT_LIMIT, longest_label(log)
        print(
            "ok   the running-call note draws the budget's own copy: what stops, "
            "what does not, and what Stop cannot do"
        )

        # And the note is *only* there while a call is running: a panel that shows
        # the budget contract over an idle input is noise, and worse, it reads as
        # if something were running.
        #
        # The fixture's last row is a permanently `running` tool (it is a static
        # transcript, so nothing will ever answer it), which is why the row itself
        # is parked rather than the turn being ended: `running_tool` is a property
        # of the transcript, and in the live loop every path out of a turn answers
        # its queued calls, so this state cannot arise there.
        parked = conversation.session.running_tool
        assert parked is not None, "the fixture should still carry its running row"
        parked.status = conversation.STATUS_OK
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        idle = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "running code" not in idle, idle
        assert "Budget:" not in idle, idle
        parked.status = conversation.STATUS_RUNNING
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        assert "running code" in " ".join(entry[1] for entry in log if entry[0] == "label")
        print("ok   and it goes again when nothing is running")
        # The turn this block opened is closed before anything else is drawn:
        # `streaming` is what decides whether the row offers Stop or Send, and the
        # checks below are about Send.
        assert conversation.session.cancel() is True

        # The sentence for a call that was actually interrupted is drawn as the
        # turn's last row. Built by the real rule, from a real verdict, so a
        # change to `budget.stopped_sentence` reaches this check.
        conversation.session.messages.append(
            conversation.Message(
                "assistant",
                kind=conversation.KIND_ERROR,
                text=budget.stopped_sentence(
                    {
                        "armed": True,
                        "kind": budget.KIND_CALL,
                        "seconds": 3.2,
                        "limit": budget.CALL_SECONDS,
                        "interrupts": 1,
                        "interrupted": True,
                        "late": False,
                        "disarmed": False,
                    }
                ),
            )
        )
        assert conversation.session.messages[-1].text.startswith(budget.MARK_STOPPED)
        log = []
        instance._draw_boxes(StubLayout(log), context, panel.wrap_budget(context), True)
        drawn = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "ran past its budget" in drawn, drawn
        assert "3.2s" in drawn, drawn
        assert longest_label(log) <= LABEL_SOFT_LIMIT, longest_label(log)
        print("ok   a stopped call's sentence draws, with the seconds it actually ran")
        conversation.session.messages.pop()

        # The two numbers are one contract, and they appear in three places: the
        # budget, the tool description the model reads, and the base prompt. Two of
        # the three are text, so they can drift without a test noticing; this is
        # that test. (The panel's copy is drawn above and comes from `budget`.)
        assert f"{budget.CALL_SECONDS:.0f}s" in execution.RUN_BLENDER_PYTHON_SCHEMA["function"]["description"]
        assert f"{budget.TURN_SECONDS:.0f}s" in execution.RUN_BLENDER_PYTHON_SCHEMA["function"]["description"]
        assert "no time limit" not in execution.RUN_BLENDER_PYTHON_SCHEMA["function"]["description"]
        assert "cannot be cancelled" not in execution.RUN_BLENDER_PYTHON_SCHEMA["function"]["description"]
        assert "15 seconds" in prompt.BASE_PROMPT, prompt.BASE_PROMPT[:40]
        assert "60" in prompt.BASE_PROMPT
        assert "force-quit" in prompt.BASE_PROMPT
        print(
            "ok   the tool description and the prompt name the same two figures "
            "as the budget, and neither still promises no time limit"
        )

        # One ledger, two halves. `run_python` catches `budget.Exceeded` by class
        # identity and the loop opens the turn on `session.limits`, so if the
        # package ever resolved two copies of `budget.py` the turn clock and the
        # call window would be two different 60 s figures - and the failure would
        # be a silently unbounded turn, not an exception anyone would see.
        assert conversation.session.limits is budget.LIMITS, conversation.session.limits
        assert execution.budget.LIMITS is budget.LIMITS, execution.budget.LIMITS
        print("ok   both halves share one budget object, so there is one 60s ledger")

        # Where the conversation is filed, and the two actions on the record
        # itself (build ticket 04). The scope on screen in *this* run is the
        # degraded one, and that is the case worth pinning: the add-on is imported
        # as a plain module here, so `extension_path_user` raises - measured, it
        # requires a package name that names an extension - and there is nowhere to
        # write. A panel that cannot say so is the failure this file exists to
        # catch.
        assert not scope.store_.available, scope.store_.directory()
        print(f"ok   no history directory in this run, and the scope says so: {scope.header()!r}")
        assert scope.header() == scope.store.SESSION_ONLY_LABEL, scope.header()
        assert "nothing is written" in scope.note(), scope.note()
        # And the other degradation - a real file path, nowhere to file it - which
        # is the case the ratified decision spelled out: degrade to in-memory and
        # say so, rather than failing or pretending.
        scope.current.blend_path = "/tmp/scene.blend"
        assert scope.header() == scope.store.NO_STORE_LABEL, scope.header()
        assert "this session only" in scope.note(), scope.note()
        scope.current.blend_path = ""
        print("ok   a file with nowhere to save says `session only` too")

        log = []
        instance._draw_header(StubLayout(log), context)
        header_labels = [entry[1] for entry in log if entry[0] == "label"]
        assert scope.header() in header_labels, log
        print("ok   the header names the scope, so `session only` is visible before it matters")

        log = []
        instance._draw_history(StubLayout(log), panel.wrap_budget(context))
        assert (
            "operator",
            "blender_copilot.reveal_history",
            "Show folder",
        ) in log, log
        assert (
            "operator",
            "blender_copilot.delete_history",
            "Delete all",
        ) in log, log
        print("ok   the history box offers the folder and the delete")

        # A pruned history says so. `meta.retention` is what the store writes, so
        # this is the panel half of "a store that prunes silently is a trust bug".
        kept = dict(scope.current.meta)
        scope.current.meta = {"retention": {"turns_dropped": 4}}
        log = []
        instance._draw_history(StubLayout(log), panel.wrap_budget(context))
        drawn = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "4 earlier turns" in drawn, log
        assert "history cap" in drawn, log
        print("ok   a pruned store admits it on screen, with the count")
        scope.current.meta = kept

        # Deleting everything asks first, and the ask is not something this file
        # can perform - it needs a window (the GUI probe drives it). What is
        # checked here is that the operator *has* an invoke, because an operator
        # without one is called straight through to `execute`, which would delete
        # everything on a mistimed Enter.
        assert "invoke" in vars(panel.BLENDER_COPILOT_OT_delete_history), "no confirmation"
        assert "cannot be undone" in panel.BLENDER_COPILOT_OT_delete_history.bl_description
        print("ok   delete-all goes through a confirmation, not straight to execute")

        # --- the undo receipt and the pause (build ticket 05) -----------------
        # The receipt was built by the real rule against a real change to this
        # run's scene (see the seeding above), and it is drawn by the real draw
        # body. It is re-made here because `begin_turn` cleared it: the stop-slot
        # check above opened a turn, and a new turn supersedes the last receipt -
        # which is the behaviour, not a leak.
        conversation.session.last_receipt = undo.receipt(
            before, after, pushed=True, label_text="copilot: add a target empty"
        )
        log = []
        instance._draw_receipt(StubLayout(log), panel.wrap_budget(context))
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "Undoable" in joined, joined
        assert "objects:" in joined, joined
        assert "EMPTY: none" in joined, joined
        assert undo.REVERT_LINE in joined, joined
        assert undo.HISTORY_LINE in joined, joined
        assert undo.COVERAGE_LINE in joined, joined
        print("ok   the receipt names what changed, the shortcut and the coverage line")
        # Wrapped, not one long label. The panel MIDDLE-CLIPS what does not fit,
        # and a clipped receipt is one whose whole job - naming what changed -
        # fails silently.
        assert longest_label(log) <= LABEL_SOFT_LIMIT, longest_label(log)

        # A refused step must not look like a step.
        conversation.session.last_receipt = undo.receipt(
            before,
            after,
            pushed=False,
            reason=undo.REFUSED_EDIT_MODE,
            label_text="copilot: add a target empty",
        )
        log = []
        instance._draw_receipt(StubLayout(log), panel.wrap_budget(context))
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "Not undoable" in joined, joined
        assert "edit mode" in joined, joined
        assert undo.REVERT_LINE not in joined, joined
        assert undo.HISTORY_LINE not in joined, joined
        assert undo.COVERAGE_LINE in joined, joined
        print("ok   a refused step says so, and never claims Ctrl+Z takes it back")

        # No step was created: no receipt box at all, rather than an empty one.
        conversation.session.last_receipt = None
        log = []
        instance._draw_receipt(StubLayout(log), panel.wrap_budget(context))
        assert log == [], log
        print("ok   a turn that changed nothing draws no receipt")
        conversation.session.last_receipt = undo.receipt(
            before, after, pushed=True, label_text="copilot: add a target empty"
        )

        # Global Undo off: the banner names the pause and offers the fix, and Send
        # is drawn disabled. Both halves, because the failure §4 exists to stop is
        # a Send that still looks pressable when nothing can be recovered.
        off = fake_context(use_global_undo=False)
        log = []
        instance._draw_header(StubLayout(log), off)
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        for line in undo.PAUSE_LINES[undo.PAUSE_GLOBAL_UNDO]:
            assert line in joined, (line, joined)
        assert (
            "operator",
            "blender_copilot.enable_global_undo",
            "Turn Global Undo on",
        ) in log, log
        print("ok   Global Undo off draws the pause banner and the one-click fix")

        log = []
        instance._draw_actions(StubLayout(log), off, panel.wrap_budget(context))
        assert send_is_disabled(log), log
        assert ("operator", "blender_copilot.send", "Send") in log, log
        print("ok   and Send is drawn disabled while auto-run is paused")

        # Edit mode pauses too - *Confirm the four undo cases in a GUI* measured
        # that a push there records nothing usable and the undo after it deletes
        # the object - and its banner is a different one with no button, because
        # the fix is the user's own Tab key.
        edit = fake_context(mode="EDIT_MESH")
        log = []
        instance._draw_header(StubLayout(log), edit)
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert undo.PAUSE_LINES[undo.PAUSE_EDIT_MODE][0] in joined, joined
        assert "Auto-run is paused" in joined, joined
        assert (
            "operator",
            "blender_copilot.enable_global_undo",
            "Turn Global Undo on",
        ) not in log, log
        log = []
        instance._draw_actions(StubLayout(log), edit, panel.wrap_budget(context))
        assert send_is_disabled(log), log
        print("ok   edit mode pauses it too, with its own banner and no button")

        # And with neither pause, no banner and a live Send.
        log = []
        instance._draw_header(StubLayout(log), context)
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "Auto-run is paused" not in joined, joined
        log = []
        instance._draw_actions(StubLayout(log), context, panel.wrap_budget(context))
        assert not send_is_disabled(log), log
        print("ok   with neither pause there is no banner and Send is live")

        # --- the trim notice and the chip (build ticket 06) -------------------
        # The demo transcript carries a note, so every variant above drew it in both
        # states. What is checked here is what is about *state* rather than layout:
        # the chip is on only while the projection really trims, the note's expander
        # exists, and the budget is a preference that can be pulled down.
        notes = [
            message
            for message in conversation.session.messages
            if message.kind == conversation.KIND_NOTE
        ]
        assert len(notes) == 1, notes
        note_index = conversation.session.messages.index(notes[0])
        notes[0].expanded = False
        log = []
        instance._draw_part(StubLayout(log), notes[0], note_index, panel.wrap_budget(context))
        collapsed = [entry[1] for entry in log if entry[0] == "label"]
        assert any("context trimmed" in line for line in collapsed), log
        assert (
            "operator",
            "blender_copilot.toggle_detail",
            "which turns",
        ) in log, log
        assert longest_label(log) <= LABEL_SOFT_LIMIT, longest_label(log)
        notes[0].expanded = True
        log = []
        instance._draw_part(StubLayout(log), notes[0], note_index, panel.wrap_budget(context))
        joined = " ".join(entry[1] for entry in log if entry[0] == "label")
        assert "budget" in joined and "turn 1:" in joined, joined
        assert longest_label(log) <= LABEL_SOFT_LIMIT, longest_label(log)
        print("ok   the trim note draws expanded and collapsed, and wraps in both")

        # The chip is derived from the record and the budget, so it appears and
        # disappears with them rather than with a flag written when trimming
        # happened - which is the whole reason it cannot be stale.
        saved_history = list(conversation.session.history)
        saved_budget = conversation.session.budget_source
        conversation.session.history[:] = [
            {"role": "user", "content": "make it taller"},
            {
                "role": "assistant",
                "content": "done, and here is a long enough explanation to matter" * 2,
            },
            {"role": "user", "content": "now make it red"},
            {"role": "assistant", "content": "done"},
        ]
        conversation.session.budget_source = lambda: 10
        log = []
        instance._draw_header(StubLayout(log), context)
        drawn = [entry[1] for entry in log if entry[0] == "label"]
        assert "\u2702 trimmed" in drawn, log
        print(f"ok   the header chip says the model is not seeing all of it: {drawn!r}")
        conversation.session.budget_source = None
        log = []
        instance._draw_header(StubLayout(log), context)
        drawn = [entry[1] for entry in log if entry[0] == "label"]
        assert "\u2702 trimmed" not in drawn, log
        print("ok   and the chip goes again when the same record fits the budget")
        conversation.session.history[:] = saved_history
        conversation.session.budget_source = saved_budget

        prop = panel.BlenderCopilotPreferences.bl_rna.properties["context_history_kib"]
        assert prop.default == 0, prop.default
        assert getattr(prop, "hard_max", None) == context_module.MAX_BUDGET_KIB, (
            getattr(prop, "hard_max", None),
            getattr(prop, "max", None),
        )
        print(
            "ok   the history budget is a preference that ships derived and can "
            f"only be pulled down (0-{prop.hard_max} KiB)"
        )

        print("\nall draw bodies ran")
    finally:
        bc.unregister()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback

        traceback.print_exc()
        # Blender exits 0 even when a `--python` script raises, so a verification
        # pipeline chaining with `&&` CANNOT gate on the exit code. That is how a
        # broken checker got committed once already: the traceback scrolled past
        # and the next step ran anyway. Print a token that can be grepped for, and
        # attempt the exit code as well in case a caller does honour it.
        print("SMOKE FAILED")
        sys.exit(1)
    print("SMOKE OK")
