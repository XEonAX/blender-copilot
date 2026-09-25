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
from blender_copilot import conversation, panel  # noqa: E402


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

        for variant in ("log", "boxes", "external"):
            for expanded in (False, True):
                for message in conversation.session.messages:
                    message.expanded = expanded
                conversation.session.newest()
                log: list = []
                layout = StubLayout(log)
                if variant == "log":
                    instance._draw_log(layout, context)
                elif variant == "external":
                    instance._draw_external(layout, context)
                else:
                    instance._draw_boxes(layout, context)
                instance._draw_header(layout, context)
                instance._draw_receipt(layout)
                instance._draw_input(layout, context, settings)
                instance._draw_actions(layout, context)
                instance._draw_coverage(layout)
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

        # A failed API call and a running tool must both be reachable by drawing.
        conversation.session.older()
        page, _, _, _ = conversation.session.page_view()
        kinds = [m.kind for m in page]
        print("ok   older page renders kinds:", sorted(set(kinds)))

        # Stop replaces Send only while streaming.
        conversation.session.send("check the stop slot")
        log = []
        instance._draw_actions(StubLayout(log), context)
        assert ("operator", "blender_copilot.stop", "Stop") in log, log
        conversation.session.cancel()

        print("\nall draw bodies ran")
    finally:
        bc.unregister()


main()
