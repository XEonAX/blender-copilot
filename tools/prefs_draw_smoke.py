#!/usr/bin/env python3
"""Does the add-on's preferences page draw, and does it say the right things?

    python3 tools/bounded_run.py 60 -- /Applications/Blender.app/Contents/MacOS/Blender \\
        --background --python tools/prefs_draw_smoke.py

**No `--factory-startup` here, and that is the point**: this needs the extension
*enabled*, and factory startup drops the user's preferences along with the
add-on. The page is the route a human takes to fill the key in, so the one thing
worth checking headlessly is that `draw()` runs at all - it is new code on the
path every user's first run goes through, and a raise inside it leaves the field
unusable while the traceback lands in the terminal.

A Panel's `draw()` cannot run without a window, but an `AddonPreferences.draw()`
is ordinary Python over a `UILayout` - the same substitution
`tools/panel_draw_smoke.py` makes for the sidebar - so this drives it against a
stub layout that records every call.

What it proves: the method runs; it binds a PASSWORD field named `api_key` and
fields for `base_url` and `model`; it says out loud that the key is plaintext in
`userpref.blend`; it shows which route a send would use; and nothing it draws is
long enough to be clipped. What it does **not** prove: how any of it *looks*.
Masking in particular is a display-layer behaviour of a real widget, measured in
ticket 05 and unobservable here - this cannot tell a password field from a text
field.

Prints `SMOKE OK` / `SMOKE FAILED`. Grep for the token: Blender exits 0 even when
a `--python` script raises.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "prefs-draw.txt"

# The same soft limit `panel_draw_smoke.py` uses, for the same measured reason:
# 104 characters in a box was middle-clipped on a real screen, 65 rendered whole,
# so 70 catches gross overflow. Absence of a long line is NOT evidence that text
# fits - the stub knows nothing about pixels.
LABEL_SOFT_LIMIT = 70

NOTES: list[str] = []
FAILURES: list[str] = []


def note(line: str) -> None:
    NOTES.append(line)
    print(f"PREFS | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


class StubLayout:
    """Records the draw calls, the way `panel_draw_smoke.py`'s does."""

    def __init__(self, log):
        self._log = log
        self.alert = False
        self.enabled = True

    def _child(self):
        return StubLayout(self._log)

    def row(self, align=False):
        self._log.append(("row", "", ""))
        return self._child()

    def box(self):
        self._log.append(("box", "", ""))
        return self._child()

    def column(self, align=False):
        return self._child()

    def split(self, factor=0.5, align=False):
        return self._child()

    def separator(self, factor=1.0):
        pass

    def label(self, text="", **kwargs):
        self._log.append(("label", text, kwargs.get("icon", "")))

    def prop(self, data, name, **kwargs):
        self._log.append(("prop", name, kwargs.get("text", "")))
        return self._child()

    def operator(self, idname, **kwargs):
        self._log.append(("operator", idname, kwargs.get("text", "")))
        return self._child()


def fake_context():
    """A context shaped like the preferences editor's, minus the window.

    `region` is absent on purpose: `wrap_budget` falls back to the fixed measure,
    which is what makes the wrap deterministic off a GUI.
    """
    return SimpleNamespace(
        preferences=SimpleNamespace(
            addons=bpy.context.preferences.addons,
            edit=SimpleNamespace(use_global_undo=True),
            system=SimpleNamespace(ui_scale=1.0),
        ),
        mode="OBJECT",
        window=None,
    )


def main() -> int:
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None:
        note(f"FAILED: {EXT} is not enabled, so there is no preferences page to draw")
        note("this probe must NOT be run with --factory-startup")
        FAILURES.append("add-on not enabled")
        return 1
    settings = addon.preferences
    if settings is None:
        note("FAILED: the add-on is enabled but exposes no preferences")
        FAILURES.append("no preferences")
        return 1

    # The three fields the credential needs, and the types that make them usable:
    # a PASSWORD widget cannot be checked here, but the property's own `subtype`
    # can, and that is the half a human's masking depends on.
    for name in ("api_key", "base_url", "model"):
        check(f"a {name!r} field exists", hasattr(settings, name))
    check(
        "the key field is a masked widget",
        settings.bl_rna.properties["api_key"].subtype == "PASSWORD",
    )
    check(
        "and nothing about it claims to be safe",
        "unencrypted" in settings.bl_rna.properties["api_key"].description.lower(),
    )

    log: list = []
    context = fake_context()
    # `self.layout` is not an attribute of the preferences *instance* and never
    # was: Blender sets it on the **class** immediately before calling `draw()`,
    # then deletes it. Verified in the installed build rather than inferred -
    # `/Applications/Blender.app/Contents/Resources/5.2/scripts/startup/bl_ui/
    # space_userpref.py:2433-2441`, `addon_preferences_class.layout = box_prefs`
    # around the call. Calling `draw()` by hand without this does not test the
    # page, it tests an object the page never sees: the first version of this
    # probe raised `AttributeError: no attribute 'layout'` and the add-on was
    # right. The real failure this mirrors is worth naming too - if it were a real
    # raise, Blender's own handler swallows it and draws a red
    # "Error (see console)" where the settings should be.
    settings_class = type(settings)
    settings_class.layout = StubLayout(log)
    try:
        settings.draw(context)
    except Exception as exc:  # noqa: BLE001 - this is the thing under test
        import traceback

        traceback.print_exc()
        note(f"FAILED: preferences draw() raised {type(exc).__name__}: {exc}")
        FAILURES.append("draw raised")
        return 1
    finally:
        del settings_class.layout

    labels = [entry[1] for entry in log if entry[0] == "label"]
    props = [entry[1] for entry in log if entry[0] == "prop"]
    operators = [entry[1] for entry in log if entry[0] == "operator"]
    note(f"drew {len(log)} widgets: {len(labels)} labels, {len(props)} props, {len(operators)} operators")

    # The three fields, bound to the real RNA properties - not to a copy, which
    # is the failure that would look right and save nothing.
    check("the key field is drawn", "api_key" in props)
    check("the base URL is drawn", "base_url" in props)
    check("the model is drawn", "model" in props)
    # The two caps. `context_history_kib` existed and was read for a whole ticket's worth
    # of work without being drawn anywhere, which made it a lever only for somebody who
    # knew the RNA name - so both are checked here rather than one.
    check("the round cap is drawn", "rounds_per_turn" in props)
    check("and so is the history budget", "context_history_kib" in props)

    joined = " ".join(labels)
    check(
        "the page says the key is stored unencrypted",
        "unencrypted" in joined and "userpref.blend" in joined,
    )
    check(
        "and offers the opt-out rather than implying safety",
        "DEEPSEEK_API_KEY" in joined,
    )
    check(
        "and warns that model names churn and a retired one is remapped",
        "retired" in joined or "silently" in joined,
    )
    check(
        "the page shows which route a send would use, key included as a fingerprint",
        "Send will use" in joined and "key:" in joined,
    )

    worst = sorted(
        ((len(text), text) for text in labels if len(text) > LABEL_SOFT_LIMIT), reverse=True
    )
    for length, text in worst:
        note(f"     !! {length} chars will likely be clipped: {text!r}")
    check("nothing drawn is grossly over the measured clipping limit", not worst)

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        note(f"wrote {LOG}")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        note(f"could not write {LOG}: {exc}")

    for failure in FAILURES:
        note(f"FAILED: {failure}")
    print("SMOKE OK" if not FAILURES else "SMOKE FAILED", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("SMOKE FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
