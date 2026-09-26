"""The Blender side of the tool surface: the prelude, the readers, and the executor.

Split from `execution.py` on purpose. Everything the model receives - the
envelope, the filters, the caps, the truncation flags, the `not_found` matches -
is decided in the bpy-free module, where the CPython suite can disagree with it.
The *only* thing that needs a live Blender is reaching into it, and that is this
file's whole job: it supplies the **world** the readers read through.

Ticket 06's prelude is the Python Console's, because that is the environment most
Blender education assumes: `bpy`, `C` for `bpy.context`, `D` for `bpy.data`,
`math`, and the four `mathutils` types a scene script reaches for first. They are
rebuilt on every call, not cached, so a call can never hold a datablock from
before an undo.

Three things about the readers' side are load-bearing rather than incidental:

  * **`objects()` evaluates the depsgraph first.** `obj.dimensions` does not move
    until it does (measured, ticket 10), so a read of a dimension that skipped the
    update would report the scene's *previous* state - the single worst thing a
    tool whose job is "tell me the truth about this scene" could do.
  * **`rna_of_operator()` checks the cached enumeration before reaching for the
    attribute.** `getattr(bpy.ops.mesh, "frobnicate")` returns a function instead
    of raising (measured, ticket 06), so existence cannot be answered by
    `getattr` at all. This is why the enumeration exists.
  * **The two caches hold names, never datablocks.** Names cannot dangle after an
    undo; a cached RNA pointer could, and would raise `ReferenceError` on the next
    call.
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


class LiveWorld:
    """The live build, seen through the small interface `execution.py` documents.

    One instance is shared (see `WORLD` below), so the two enumerations are built
    at most once per Blender session. Both are of *names* and neither depends on
    the scene, which is what makes caching them safe across an undo.
    """

    def __init__(self) -> None:
        self._type_names: list[str] | None = None
        self._operator_names: list[str] | None = None

    # -- the prelude ---------------------------------------------------------
    def bindings(self) -> dict:
        return bindings()

    # -- the scene -----------------------------------------------------------
    def facts(self) -> dict:
        """The scalar scene facts ticket 06's summary scope is made of."""
        context = bpy.context
        scene = context.scene
        filepath = bpy.data.filepath
        return {
            "scene": getattr(scene, "name", None),
            "filepath": filepath or None,
            # `is_saved` is inferred from a non-empty filepath because that is the
            # only thing Blender exposes: a never-saved file has none, and a saved
            # one may still be dirty (which `is_dirty` answers separately).
            "is_saved": bool(filepath),
            "is_dirty": bool(bpy.data.is_dirty),
            "mode": str(context.mode),
            "frame": getattr(scene, "frame_current", None),
            "frame_range": [
                getattr(scene, "frame_start", None),
                getattr(scene, "frame_end", None),
            ],
            "render_engine": getattr(getattr(scene, "render", None), "engine", None),
            "unit_system": getattr(getattr(scene, "unit_settings", None), "system", None),
            "global_undo": bool(context.preferences.edit.use_global_undo),
        }

    def objects(self) -> list:
        """Every object in the file, after a depsgraph update.

        The update is not optional: dimensions, and anything else derived from the
        evaluated state, is stale without it (ticket 10's read-back trap).
        """
        try:
            bpy.context.view_layer.update()
        except Exception:  # noqa: BLE001 - a read is never allowed to fail the call
            pass
        return list(bpy.data.objects)

    def active_name(self):
        active = bpy.context.view_layer.objects.active
        return active.name if active is not None else None

    def selected_names(self) -> list[str]:
        return [obj.name for obj in bpy.context.selected_objects]

    def collections(self) -> list:
        """The scene's top-level collections, as a nested {name, count, children}.

        Built from `scene.collection.children` rather than from `bpy.data`,
        because the question being asked is "what is in my file *here*", and a
        collection outside the scene is not in it.
        """
        scene = bpy.context.scene
        if scene is None:
            return []

        def build(collection) -> dict:
            return {
                "name": collection.name,
                "count": len(collection.objects),
                "children": [build(child) for child in collection.children],
            }

        return [build(child) for child in scene.collection.children]

    # -- the RNA enumerations ------------------------------------------------
    def type_names(self) -> list[str]:
        if self._type_names is None:
            self._type_names = sorted(
                name for name in dir(bpy.types) if not name.startswith("_")
            )
        return self._type_names

    def operator_names(self) -> list[str]:
        """Every registered operator id, by walking `dir(bpy.ops)` one level deep.

        2,498 ids in 0.035 s on the installed 5.2.2 (measured, ticket 06), built
        once. `getattr` cannot answer existence here: it returns a function for a
        name that does not exist.
        """
        if self._operator_names is None:
            names: list[str] = []
            for group_name in dir(bpy.ops):
                if group_name.startswith("_"):
                    continue
                group = getattr(bpy.ops, group_name, None)
                if group is None:
                    continue
                for op_name in dir(group):
                    if not op_name.startswith("_"):
                        names.append(f"{group_name}.{op_name}")
            self._operator_names = sorted(names)
        return self._operator_names

    def rna_of_type(self, name: str):
        target = getattr(bpy.types, name, None)
        return getattr(target, "bl_rna", None)

    def rna_of_operator(self, idname: str):
        operator = self._operator(idname)
        get_rna_type = getattr(operator, "get_rna_type", None)
        if not callable(get_rna_type):
            return None
        try:
            return get_rna_type()
        except Exception:  # noqa: BLE001 - a broken intro is a not_found, not a crash
            return None

    def operator_poll(self, idname: str):
        poll = getattr(self._operator(idname), "poll", None)
        if not callable(poll):
            return None
        try:
            return bool(poll())
        except Exception:  # noqa: BLE001 - poll() can decline as well as fail
            return None

    def _operator(self, idname: str):
        """The operator object for an id, or None - enumeration first."""
        if not isinstance(idname, str) or idname not in self.operator_names():
            return None
        group_name, _, op_name = idname.partition(".")
        if not op_name:
            return None
        group = getattr(bpy.ops, group_name, None)
        return getattr(group, op_name, None) if group is not None else None


# One instance for the session: it holds only the two name caches above.
WORLD = LiveWorld()


def execute(call: dict) -> dict:
    """Run one tool call and hand the loop an opaque result dict.

    The contract is `conversation.Conversation.attach`'s: `ok`, `content`,
    `detail`, `summary`. No exception leaves here - a broken call is a *result*,
    which is the whole failure policy in one sentence.
    """
    return execution.execute_tool(call, WORLD)
