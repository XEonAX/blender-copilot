"""What a turn sends the model: the stable base prompt, and the live summary.

This is ticket 10's prompt, delivered now that all three tools exist. Where its
content sits, and why, is ticket 10's placement rule - standing obligations the
runtime cannot enforce go in the system prompt (this file), facts that only matter
while calling one tool go in that tool's description (`execution.py`), anything
that changes with the scene or the build goes through `get_rna_info`, and the
long tail of idioms goes in the guides (`guides.py`).

Three deliberate departures from ticket 10's draft, all of them sequencing rather
than disagreement, and all of them to be revisited when build ticket 05 lands:

  * its `{capability}` line is **gone**, as the map requires: the capability
    restriction was rejected, so the prompt has no restriction to describe. The
    anti-drift rule survives it - the prompt must never describe a boundary the
    runtime does not enforce.
  * the sentence "the loop pushes that step at the end of the turn" is not here,
    and neither is "The loop also diffs the scene before and after the turn, and
    its receipt will contradict a false claim." Both assert mechanisms that build
    ticket 05 owns and that do not exist yet, and a prompt that promises an
    enforcement it does not have is the same failure the `{capability}` line was
    removed for. The *obligations* those sentences existed to enforce - never
    calling the undo operators yourself, and reading a change back before
    claiming it - are both stated, and are stated without the mechanism.

Placement of the two messages, per ticket 14's correction to ticket 09 §4 (and
confirmed with the provider in ticket 16): the base prompt is stable text at
index 0, so the provider's cache prefix holds across turns, and the live summary
is a *trailing* `system` message, recomputed per request. It is authoritative at
capture and stale the moment anything mutates, and the prompt says so.

This module needs `bpy` for the summary, so the wire-message assembly it feeds
lives in `conversation.py` instead - which keeps that half testable on plain
CPython and usable by `tools/transport_smoke.py`.
"""

from __future__ import annotations

import json

import bpy

from . import execution, toolbox

BASE_PROMPT = """You are Blender Copilot, an agent running inside Blender 5.2. You \
change the user's scene by writing Python that this addon executes in Blender's own \
process. You cannot see the viewport, the UI, or any render.

TOOLS. run_blender_python executes code; get_scene_info reads the scene; \
get_rna_info verifies a Blender API name. A tool result is the only evidence a \
change happened - not your intent, and not the absence of an error.

EACH TURN you receive a live scene summary (the get_scene_info `summary` scope), in \
the final system message. It is the ground truth for selection, active object, mode \
and counts - prefer it to your memory and to the user's description of what is \
selected. It is captured for that request, so any mutation makes it stale: read \
back before asserting the new state.

TARGET RESOLUTION. Resolve "this", "it", "the object" in this order: (1) a name or \
reference in the user's message; (2) the selection, if exactly one object; (3) the \
active object. Name the resolved target in the call's `purpose`, and in prose when \
the phrasing was ambiguous. If several objects are selected, or there is no active \
object, or two candidates remain - ask, and ask before the first mutating call by \
replying with no tool call at all. Do not mutate on a guess.

CHANGE DISCIPLINE. One meaningful change per run_blender_python call. Each call is a \
separately labelled step in the transcript, so a wrong or failing call stays bounded \
and identifiable. Do not bundle unrelated edits into one script. Ctrl+Z reverts the \
whole turn, not one call, so never call bpy.ops.ed.undo* / redo / undo_history / \
undo_push yourself, and never pass undo=True to an operator - that splits the turn \
into steps the user cannot reason about.

READ BACK. Before claiming a change succeeded, observe it - print the value you \
changed. obj.dimensions does not update until the depsgraph does, so call \
bpy.context.view_layer.update() before reading it. A call that raised nothing is not \
proof: operators can return {'CANCELLED'} as a silent no-op.

API DISCIPLINE. Before writing any bpy name you are not certain exists in 5.2, call \
get_rna_info. Never guess an operator, property or socket name. Reach node sockets by \
name, not by index, and list the names at runtime when unsure. Prefer the data API to \
an operator when both exist (e.g. mesh.materials.append, \
bpy.data.objects.remove(do_unlink=True)) - operators need context this addon cannot \
always supply. When an operator's poll fails, read the RuntimeError, set the missing \
context once or switch to the data API, and if neither works, report it instead of \
retrying.

CAPABILITY. Your code runs synchronously on Blender's main thread: it cannot be \
cancelled or time-limited, and Blender's UI is frozen while it runs, so `while True:` \
or a blocking call must be force-quit. Keep every script short and bounded. Never use \
input(), and never register a timer, handler or background loop. Only bpy.data can be \
recovered (one Ctrl+Z per turn). Files, preferences, network, render/GPU state and \
Python state cannot. If a request needs a capability you do not have, say so plainly \
and stop; the user decides.

STYLE. Report what you did in one or two sentences, citing observed values. Do not \
narrate a plan at length - call the tool. Short code, no commentary-as-code. Your \
prose is rendered as plain text in a narrow panel with no headings and no lists, so \
keep lines short and replies brief."""

MAX_SELECTION_NAMES = 8


def live_summary() -> str:
    """The turn's live scene summary: ticket 06's `summary` object, serialised.

    **One definition, not two.** This is the same object `get_scene_info` returns
    for `scope="summary"` (`execution.scene_summary`), with `captured` saying it
    was taken at the start of the turn. A hand-written summary here would be a
    second schema for the same facts, and the two would eventually disagree -
    which is worse than having no summary at all, because the prompt tells the
    model this one outranks its memory.

    Never raises: a turn must still go out if the scene cannot be read, because
    "the summary failed" is not a reason the user's prompt should vanish. The
    failure is reported *in* the summary, where the model can see it and call
    `get_scene_info` itself.
    """
    try:
        fields = execution.scene_summary(toolbox.WORLD, captured="turn_start")
    except Exception as exc:  # noqa: BLE001 - the turn goes out regardless
        fields = {
            "unavailable": type(exc).__name__,
            "note": (
                "The live scene summary could not be read. Call get_scene_info "
                "before assuming anything about the scene."
            ),
        }
    return json.dumps(fields, ensure_ascii=False, default=str)


def messages_for(session, user_text: str) -> list[dict]:
    """The request body for one turn, with the scene read now."""
    base_prompt, summary = context()
    return session.wire_messages(user_text, base_prompt, summary)


def context() -> tuple[str, str]:
    """`(base_prompt, live_summary)`, read fresh - the loop's `context` hook.

    A pair rather than two calls because the two must belong to the same moment: a
    summary computed before a mutation and sent after it would be worse than no
    summary at all, and the prompt says the summary is authoritative at capture.
    """
    return BASE_PROMPT, live_summary()
