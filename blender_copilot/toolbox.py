"""The Blender side of the tool surface: the prelude, and the executor the loop calls.

Split from `execution.py` on purpose. The machinery - fresh namespace, capture,
traceback, caps - is bpy-free, so it is checked on plain CPython with fake
bindings; the *only* thing that needs a live Blender is the set of names model
code is handed, and that is this file's whole job.

Ticket 06's prelude is the Python Console's, because that is the environment most
Blender education assumes: `bpy`, `C` for `bpy.context`, `D` for `bpy.data`,
`math`, and the four `mathutils` types a scene script reaches for first. They are
rebuilt on every call, not cached, so a call can never hold a datablock from
before an undo.

The other two tools (`get_scene_info`, `get_rna_info`) land here as well; this
pass ships the one the tracer bullet needs.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from . import execution

# Re-exported so the transport layer has one import for "what the model may call"
# and never has to reach into the bpy-free module for it.
SCHEMAS = execution.SCHEMAS


def bindings() -> dict:
    """The prelude names, read fresh from the running session."""
    return {
        "bpy": bpy,
        "C": bpy.context,
        "D": bpy.data,
        "math": math,
        "Vector": Vector,
        "Matrix": Matrix,
        "Euler": Euler,
        "Quaternion": Quaternion,
    }


def execute(call: dict) -> dict:
    """Run one tool call and hand the loop an opaque result dict.

    The contract is `conversation.Conversation.attach`'s: `ok`, `content`,
    `detail`, `summary`. No exception leaves here - a broken call is a *result*,
    which is the whole failure policy in one sentence.
    """
    return execution.execute_tool(call, bindings)
