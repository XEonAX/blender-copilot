#!/usr/bin/env python3
"""One real turn driven through the panel's own operators, in a real GUI session.

    set -a; . ./.env; set +a
    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender \\
        --python tools/panel_round_trip.py

**Why this one needs a screen** (screens allowed, AGENTS.md 2026-09-26; this case
would have qualified under the older narrower rule too): `bpy.app.timers` pump
only from the GUI event loop, so the drain between the worker subprocess and the
panel cannot be exercised under `blender -b` *at all*. Everything else here has a
headless counterpart in `tools/transport_smoke.py`; this run exists solely to
prove that the timer fires, drains the pipe, and repaints a real region.

It does **not** take a screenshot, because counting repaints is a mechanism claim
and pixels are not needed to make it - not because looking is off limits. It
could: a screenshot is evidence like any other now, to be cited along with what
it does not show.

It writes its verdict to `logs/panel-round-trip.txt`, prints a token, and quits
Blender itself, so the deadline is a backstop and not the mechanism. It runs
against the user's own config, where the extension is enabled as
`bl_ext.user_default.blender_copilot` (verified present, symlinked at this repo).
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import bpy

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "panel-round-trip.txt"
REPLY_DEADLINE = 90.0
TICK = 0.1

NOTES: list[str] = []
STARTED = time.monotonic()


def note(line: str) -> None:
    NOTES.append(line)
    print(f"TURN | {line}", flush=True)


def finish(ok: bool, reason: str) -> None:
    note(("RESULT " if ok else "FAILED ") + reason)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"TURN | could not write {LOG}: {exc}", flush=True)
    print("SMOKE OK" if ok else "SMOKE FAILED", flush=True)
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        sys.exit(0 if ok else 1)


def main() -> None:
    try:
        bc = importlib.import_module(EXT)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        finish(False, f"the extension is not importable as {EXT}: {exc!r}")
        return

    # Count the repaints. `stream._tick` looks the function up as a module global,
    # so replacing it here counts exactly the redraws the real drain performs.
    redraws: list[int] = []
    original_redraw = bc.stream.tag_view3d_redraw

    def counting_redraw() -> int:
        tagged = original_redraw()
        redraws.append(tagged)
        return tagged

    bc.stream.tag_view3d_redraw = counting_redraw

    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None or addon.preferences is None:
        finish(False, "no preferences: the extension is not registered as an add-on")
        return
    preferences = addon.preferences

    note(f"global undo on: {bpy.context.preferences.edit.use_global_undo}")
    if not bpy.context.preferences.edit.use_global_undo:
        finish(False, "Global Undo is off, so Send is disabled by design")
        return

    preferences.prompt_text = "Reply with the single word: pong"
    result = bpy.ops.blender_copilot.send()
    note(f"send() -> {result}")
    if "FINISHED" not in result:
        finish(False, f"Send refused: {result}")
        return

    session = bc.conversation.session
    note(f"turn opened, streaming={session.streaming}, status={session.status!r}")

    state = {"done": False}

    def poll():
        """The harness's own timer: it only watches, the add-on's drain does the work."""
        if state["done"]:
            return None

        if not session.streaming and session.status == "idle":
            state["done"] = True
            # One extra tick, so the final events are drained and drawn.
            reply = session.messages[-1] if session.messages else None
            text = reply.text if reply is not None else ""
            kinds = [message.kind for message in session.messages]
            note(f"transcript kinds: {kinds}")
            note(f"reply ({len(text)} chars): {text.strip()[:200]!r}")
            note(f"repaints: {len(redraws)} calls, {sum(1 for n in redraws if n)} tagged a region")

            if not text.strip():
                finish(False, "the turn finished with no visible reply")
            elif bc.conversation.KIND_ERROR in kinds:
                finish(False, "the turn produced an error block")
            elif not redraws:
                finish(False, "the timer never repainted: the drain did not run")
            elif not any(redraws):
                finish(False, "tag_redraw ran but found no region to tag")
            else:
                finish(True, f"one real turn, {len(text)} characters, streamed and repainted")
            return None

        if time.monotonic() - STARTED > REPLY_DEADLINE:
            state["done"] = True
            note(f"last status: {session.status!r}")
            finish(False, f"the turn did not finish within {REPLY_DEADLINE:g}s")
            return None
        return TICK

    bpy.app.timers.register(poll, first_interval=TICK)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - a token is the only gate that works
        import traceback

        traceback.print_exc()
        finish(False, f"the harness raised: {type(exc).__name__}: {exc}")
