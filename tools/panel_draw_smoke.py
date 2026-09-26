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
from blender_copilot import conversation, panel, scope  # noqa: E402


class StubLayout:
    def __init__(self, log):
        self._log = log
        self.alert = False
        self.enabled = True
        self.alignment = ""

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


def fake_context():
    return SimpleNamespace(
        preferences=SimpleNamespace(edit=SimpleNamespace(use_global_undo=True)),
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
        settings = SimpleNamespace(layout_variant="boxes", prompt_text="")

        # The live session starts EMPTY now that Send runs a real turn, so the
        # layout fixture is seeded here instead of by `register()`. One
        # conversation holding every kind the panel can render, which is what
        # these draw bodies exist to exercise.
        conversation.session.messages.clear()
        conversation.session.messages.extend(conversation._demo())
        conversation.session.last_receipt = {
            "summary": "1 object changed - Cube scaled on Z",
            "coverage": "Ctrl+Z reverts that turn; Ctrl+Alt+Z opens Blender's undo history",
        }

        for variant in ("log", "boxes", "external"):
            for expanded in (False, True):
                for message in conversation.session.messages:
                    message.expanded = expanded
                log: list = []
                layout = StubLayout(log)
                # The stub context has no `region`, so wrap_budget falls back to
                # the fixed measure - which is the point: this proves the draw
                # bodies RUN, not that they fit. Fitting is checked by
                # long_labels/longest_label below and, ultimately, by an eye.
                budget = panel.wrap_budget(context)
                if variant == "log":
                    instance._draw_log(layout, context)
                elif variant == "external":
                    instance._draw_external(layout, context)
                else:
                    instance._draw_boxes(layout, context, budget)
                instance._draw_header(layout, context)
                instance._draw_transport(layout, budget)
                instance._draw_receipt(layout, budget)
                instance._draw_input(layout, context, settings)
                instance._draw_actions(layout, context, budget)
                instance._draw_coverage(layout, budget)
                instance._draw_variant_picker(layout, settings)
                worst = long_labels(log)
                longest = longest_label(log)
                print(
                    f"ok   variant={variant:8s} expanded={expanded!s:5s} "
                    f"widgets={len(log)} boxes={log.count('box')} "
                    f"longest_label={longest}"
                )
                # Advisory and deliberately blunt: only gross overflow is caught,
                # and the bare-column case above slips past it entirely. Absence
                # of a line here is NOT evidence that the text fits.
                for length, kind, text in worst[:4]:
                    print(
                        f"     !! {length} chars will likely be clipped "
                        f"({kind}): {text!r}"
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
        instance._draw_boxes(StubLayout(log), context, panel.wrap_budget(context))
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
