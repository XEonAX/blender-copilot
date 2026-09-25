"""What a turn sends the model.

**This is not ticket 10's prompt, deliberately.** Ticket 10's base prompt teaches
three tools and their contracts; the tools are not built yet, and its own
anti-drift rule says the prompt must describe the *enforced* boundary and never a
hoped-for one. A prompt that promises `run_blender_python` while no such tool is
declared would produce a model that talks about code it cannot run - worse than a
smaller prompt that tells the truth. The full text lands with the tools pass; the
target-resolution and style rules below are the parts of ticket 10 that survive.

Placement, per ticket 10:

  * the **base prompt** is stable text at index 0, so the provider's cache prefix
    holds across turns;
  * the **live summary** is a *trailing* `system` message, recomputed per request
    (ticket 14's correction to ticket 09 §4, accepted by the provider in ticket
    16). It is authoritative at capture and stale the moment anything mutates,
    so the prompt says so.

This module needs `bpy` for the summary, so the wire-message assembly it feeds
lives in `conversation.wire_messages` instead - which keeps that half testable on
plain CPython and usable by `tools/transport_smoke.py`.
"""

from __future__ import annotations

import bpy

BASE_PROMPT = """You are Blender Copilot, an agent living inside Blender 5.2 as a chat panel in \
the 3D Viewport sidebar.

WHAT YOU CAN DO RIGHT NOW. Talk, and nothing else. This build has no tools: you \
cannot run Python, read the scene, or look up the Blender API, and that machinery \
is being built next. The only ground truth you have is the live scene summary in \
the final system message. You cannot see the viewport, the interface, or any \
render.

Be plain about that. If the user asks for a change, describe exactly what you \
would do and say that code execution is not wired up yet. Never imply you have \
already changed something.

TARGET RESOLUTION. For "this", "it" or "the object": first a name the user gave, \
then the active object, then the selection if it holds exactly one object. If \
several objects are selected, or nothing is, ask instead of guessing. Name the \
target you resolved.

STYLE. One or two short sentences unless asked for more. No headings and no \
lists. Your prose is rendered as plain text in a narrow panel that cannot scroll \
sideways, so keep lines short and replies brief."""

MAX_SELECTION_NAMES = 8


def live_summary() -> str:
    """The bounded present-tense scene fact ticket 09 §4 leans on.

    Never raises: a turn must still go out if the scene cannot be read, because
    "the summary failed" is not a reason the user's prompt should vanish.
    """
    try:
        context = bpy.context
        objects = bpy.data.objects
        active = context.view_layer.objects.active
        selected = list(context.selected_objects)
        mode = str(context.mode)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return (
            f"Live scene: unavailable ({type(exc).__name__}). "
            "No selection or active object is known."
        )

    names = [obj.name for obj in selected[:MAX_SELECTION_NAMES]]
    hidden = len(selected) - len(names)
    if hidden > 0:
        names.append(f"+{hidden} more")

    active_name = f"{active.name} ({active.type})" if active is not None else "none"
    selection = f"{len(selected)}" + (f" ({', '.join(names)})" if names else "")
    return (
        f"Live scene: {len(objects)} objects, mode {mode}, "
        f"active {active_name}, selection {selection}."
    )


def messages_for(session, user_text: str) -> list[dict]:
    """The request body for one turn, with the scene read now."""
    return session.wire_messages(user_text, BASE_PROMPT, live_summary())
