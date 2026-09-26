"""The long tail, served on demand: `get_rna_info(kind="guide", name=...)`.

Ticket 10 decided *where* guidance lives and this is the fourth of its four
surfaces: the stable system prompt, the per-tool descriptions, runtime RNA
introspection, and this - long-tail idioms that are not worth paying for on every
turn and are not interface, so they belong in neither of the first two.

Consequences of that placement, all deliberate:

  * **Content lives in the extension, not in a `Text` datablock.** A datablock
    would need `open`/`Text.read` put back into the exec namespace for our own
    docs' sake, which is a wider boundary for no gain (ticket 10, rejected
    alternatives).
  * **Versioned by the extension.** `blender_version_min` is 5.2.0 in the
    manifest; every fact below was measured on the installed 5.2.2, so the guide
    travels with the build it describes.
  * **Static.** The model cannot mutate these, so a guide cannot lie about
    itself later in the session.
  * **Not a fourth tool.** `kind="guide"` amends ticket 06's `kind` enum rather
    than adding a tool, which is what keeps the map's binding "three tools" true.

`TOPICS` is the registry's own key order, and the `get_rna_info` description is
*generated* from it, so the list the model reads cannot drift from the list that
exists. That is also why every topic has a one-line `summary`: it is what the
description and the `not_found` matches show, while `body` is what a retrieval
returns.

This module imports nothing at all, on purpose: `execution.py` has to be loadable
by path (the CPython suite and `tools/loop_wire_probe.py` both do that), and a
module with no imports is loadable from anywhere.
"""

from __future__ import annotations

# The schema version of the guide registry, reported with every guide so a
# future shape change is visible rather than guessed at.
GUIDES_VERSION = 1

GUIDES: dict[str, dict] = {
    "socket-access": {
        "summary": "Reach node sockets by name, never by index - list them at runtime.",
        "body": """Node sockets: by name, never by index.

Index order is not a contract. It changes between Blender versions and between
node types, and it is exactly the kind of memory an LLM gets wrong most
confidently. The 5.2.2 Principled BSDF set is `Base Color, Metallic, Roughness,
IOR, Alpha, ..., Specular IOR Level, ..., Transmission Weight, ..., Emission
Color, Emission Strength, ...` - note `Specular IOR Level` and `Transmission
Weight`, not the spellings most training text uses.

Do:
    node = D.materials["Mat"].node_tree.nodes["Principled BSDF"]
    print([s.name for s in node.inputs])          # list first
    node.inputs["Base Color"].default_value = (1, 0, 0, 1)

Do not:
    node.inputs[0].default_value = ...            # index is a guess

Verify a socket name with get_rna_info(kind="type", name="ShaderNodeBsdfPrincipled",
filter="inputs") before writing it, or just print the names - a print costs one
round and cannot be wrong.""",
    },
    "context-and-mode": {
        "summary": "Poll failure vs silent CANCELLED, and when to stop fighting an operator.",
        "body": """Operators, context, and the two ways one does nothing.

1. A failed poll raises. `RuntimeError: Operator bpy.ops.object.mode_set.poll()
   Context missing active object`. That text arrives in the result's `error`.
2. A passed poll can still do nothing. `object.delete()` with nothing selected
   returns `{'CANCELLED'}` with no error at all. So a call that raised nothing is
   not proof that anything happened: check the return set, and read the value
   back.

`bpy.context.temp_override(...)` exists and *satisfies* `poll()` - verified in
5.2.2: inside an override of `active_object`/`selected_objects`,
`object.mode_set.poll()` flips False to True and `mode_set(mode='EDIT')` returns
`{'FINISHED'}`. It is a poll-satisfier, not a context switch: `bpy.context.mode`
stayed `OBJECT` inside the override and after the block. So never treat
`{'FINISHED'}` as evidence that the state changed; read the state.

When an operator keeps refusing: read the RuntimeError, satisfy the one missing
context it names, and if that does not work, switch to the data API rather than
retrying. `bpy.data.objects.new`, `collection.objects.link`,
`bpy.data.objects.remove(do_unlink=True)` and `mesh.materials.append` reach the
same result without needing a context this addon cannot always supply.""",
    },
    "data-vs-operators": {
        "summary": "Operator-free equivalents, and link vs unlink vs destroy.",
        "body": """The data API usually beats the operator, and linking has three outcomes.

Prefer the data API when both exist: it needs no context, so it cannot fail a
poll, and it is legible in a transcript.

Linking semantics, three different outcomes - choose deliberately:
    collection.objects.unlink(obj)          # out of the scene; datablock SURVIVES
    bpy.data.objects.remove(obj)            # datablock orphaned (0 users)
    bpy.data.objects.remove(obj, do_unlink=True)   # datablock DESTROYED

Verified in 5.2.2: `mesh.materials.append(mat)` links slot 0 from zero slots -
no operator, no context, no poll.
    mesh = C.active_object.data
    mesh.materials.append(D.materials["Red"])

A datablock with zero users is not freed until the file is saved and reloaded,
but it is already gone from the user's point of view - do not use "it is only
orphaned" as an excuse to leave junk behind.""",
    },
    "units-and-axes": {
        "summary": "Z-up metres, scale vs dimensions, and never reinterpreting units silently.",
        "body": """Units, axes, and the two different "sizes".

Z-up, right-handed, metres. A factory scene has `unit_settings.system ==
'METRIC'`, `scale_length == 1.0`, `length_unit == 'METERS'`.

`scale` is a per-object multiplier; `dimensions` is the world-space bounding box,
includes modifiers, and is **stale until the depsgraph runs**:
    C.active_object.scale.z *= 2
    C.active_object.dimensions    # still (2, 2, 2) - the old value
    C.view_layer.update()
    C.active_object.dimensions    # (2, 2, 4)

Use `scale` when you want to change something by a ratio, `dimensions` when the
user gave you a measurement. `location` is the object origin, in world units
unless the object has a parent - check `obj.parent` before assuming.

Rule: a bare number in a request is in the scene's current units. If the user's
units are ambiguous or the scene is not metric, say which unit you assumed in the
call's `purpose` and in the reply. Never silently rescale or reinterpret a
number.""",
    },
    "read-back": {
        "summary": "Print the observable delta - it is the only evidence a change happened.",
        "body": """Read back, and print what you read.

The rule: after any mutation, print the value that changed. That print is the
evidence line in the transcript, and it is also what makes a silent no-op
visible.

    before = obj.dimensions.z
    obj.scale.z *= 1.5
    C.view_layer.update()
    print(f"{obj.name}: height {before:.2f} -> {obj.dimensions.z:.2f}")

Useful things to print, in rough order of honesty: `dimensions`, `location`,
`scale`, counts (`len(obj.modifiers)`, `len(mesh.vertices)`), names
(`[m.name for m in obj.modifiers]`, material slot names), and `obj.data.name` for
linked data.

Do not print an intention (`print("done")`) - it is unfalsifiable, and the model
that wrote it is the least reliable witness that it happened.""",
    },
    "version-churn": {
        "summary": "Any name from memory is unverified; check it at runtime.",
        "body": """Blender's API churns. Treat every `bpy` name you did not just read
as unverified.

How to check, in order of cost:

1. `get_rna_info(kind="type", name="Object", filter="material")` - properties and
   `bl_rna.functions`, against the live build.
2. `get_rna_info(kind="operator", name="object.modifier_add")` - the operator's
   signature, defaults, enum items and whether it polls right now.
3. `get_rna_info(kind="search", name="modifier")` - substring over the cached
   operator list, when you cannot remember the id.

Traps, measured in 5.2.2:
  * `hasattr` on an RNA type is an **invalid test** - `hasattr(bpy.types.Object,
    "frobnicate")` answers what you want it to answer. Use
    `bpy.types.Object.bl_rna.functions`, or `bl_rna.properties`.
  * `getattr(bpy.ops.mesh, "frobnicate")` returns a function instead of raising,
    so operator existence is checked against the enumeration, not `getattr`.
  * `bpy.types.MESH_OT_*` is not reachable; operators are only at `bpy.ops`.

If a name does not verify, say so and stop - do not try variants until one
"seems" to work.""",
    },
    "no-undo-zone": {
        "summary": "Undo reaches local bpy.data only; say so rather than promising durability.",
        "body": """What Ctrl+Z can and cannot cover - say it accurately.

Covered: local `bpy.data` only - datablocks in the running .blend, from the point
where the addon pushed an undo step. One step per agent turn, not per tool call,
so Ctrl+Z reverts the whole turn at once.

Not covered, by anything:
  * files on disk - saving, export, anything written or deleted;
  * the network, subprocesses, and anything a subprocess did;
  * Blender preferences, including the API key this addon keeps;
  * timers and handlers registered by code;
  * Python state. Variables from an earlier call do not exist, and undoing the
    scene cannot free an object the code still holds a reference to.

Two degraded states worth knowing:
  * Global Undo off in Preferences (`bpy.context.preferences.edit.use_global_undo`)
    means there is **no** recovery at all, not a smaller one. The addon refuses to
    auto-run in that state rather than pretending otherwise.
  * An object in edit mode makes a push record nothing usable - the addon refuses
    to push there.

So: after a change confined to the scene, say one Ctrl+Z reverts the turn. After
anything that left the .blend - a save, a file, a request - do not imply it can be
undone; name what cannot.""",
    },
}

# Declared order, so the generated list the model reads is stable between runs
# and a diff of the description is a diff of the registry.
TOPICS: list[str] = list(GUIDES)


def topic_list() -> str:
    """The topics, one per line, as the tool description and matches show them.

    Generated rather than hand-written: ticket 10 requires that the list the
    description carries cannot drift from the registry that serves it.
    """
    return "\n".join(f"{topic} - {GUIDES[topic]['summary']}" for topic in TOPICS)


def get(topic: str) -> dict | None:
    """One guide, or None. Exact topic only - the caller owns the not-found."""
    if not isinstance(topic, str):
        return None
    return GUIDES.get(topic.strip())
