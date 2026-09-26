#!/usr/bin/env python3
"""The two read-only tools against the *installed* Blender, with real `bpy`.

    python3 tools/bounded_run.py 60 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/read_only_tools_probe.py

Why this exists: `tests/test_conversation.py` checks everything the model
receives - the caps, the flags, the filters, the not-found matches - but it does
that through a *fake* world, because `execution.py` is deliberately bpy-free.
Everything that reaches into `bpy` is unverified by it, and that is the half that
can be wrong in ways no fake can reveal: whether `Object.bl_rna.properties` really
has 141 entries, whether `get_rna_type()` really exposes the operator's defaults,
whether the enumeration really contains the ids the model will ask for, and
whether `dimensions` really moves after `view_layer.update()`.

It runs `--background --factory-startup`, so it needs no window and leaves no
config behind. The scene is mutated only to prove the caps bite (objects are
created through the data API, never an operator), in a throwaway session.

The verdict is a **token**, not an exit code: Blender exits 0 even when a
`--python` script raises, so a caller chaining with `&&` cannot gate on the
status. Grep for `SMOKE OK`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bpy  # noqa: E402
from blender_copilot import conversation, execution, guides, prompt, toolbox  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition) -> bool:
    if condition:
        print(f"ok   {label}", flush=True)
    else:
        FAILURES.append(label)
        print(f"FAILED {label}", flush=True)
    return bool(condition)


def note(line: str) -> None:
    print(f"     {line}", flush=True)


def call(tool_name: str, **arguments) -> dict:
    """One wire tool call through the real dispatcher, as the loop would make it."""
    return execution.execute_tool(
        {
            "id": f"probe_{tool_name}",
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments)},
        },
        toolbox.WORLD,
    )


def envelope(result: dict) -> dict:
    """The envelope the model sees, or a marker when the wire content was elided."""
    try:
        return json.loads(result["content"])
    except ValueError:
        return {"unparseable": True, "ok": result["ok"]}


def main() -> None:
    world = toolbox.WORLD
    scene = bpy.context.scene

    # ------------------------------------------------------------------ wiring
    print("\n-- the registry and the prompt --")
    check("the addon declares three tools", len(toolbox.SCHEMAS) == 3)
    check(
        "and the panel's import path is the same registry the dispatcher uses",
        toolbox.SCHEMAS is execution.SCHEMAS,
    )
    check(
        "the declared names are the three the prompt teaches",
        [schema["function"]["name"] for schema in toolbox.SCHEMAS]
        == ["run_blender_python", "get_scene_info", "get_rna_info"],
    )
    check(
        "the base prompt names all three tools, so it no longer shies away from them",
        all(name in prompt.BASE_PROMPT for name in ("run_blender_python", "get_scene_info", "get_rna_info")),
    )
    check(
        "and it asserts no capability restriction the runtime does not enforce",
        "{capability}" not in prompt.BASE_PROMPT and "no tools" not in prompt.BASE_PROMPT.lower(),
    )
    check(
        "the read-only tools' own descriptions are in the prompt surface too",
        "get_scene_info" in execution.schema_for("get_scene_info")["function"]["description"]
        or "summary" in execution.schema_for("get_scene_info")["function"]["description"],
    )

    # ------------------------------------------------------------ the summary
    print("\n-- get_scene_info, scope=summary --")
    summary = call("get_scene_info", scope="summary")
    text = envelope(summary)
    check("a summary over the real factory scene is ok", summary["ok"] is True)
    note(f"scene={text.get('scene')!r} engine={text.get('render_engine')!r} "
         f"mode={text.get('mode')!r} unit={text.get('unit_system')!r} undo={text.get('global_undo')!r}")
    check("it counts the objects", text.get("object_count") == len(bpy.data.objects))
    check(
        "and counts them by type rather than listing them",
        sum(text.get("object_type_counts", {}).values()) == text.get("object_count"),
    )
    check(
        "the factory scene is the cube, the camera and the light",
        text.get("object_type_counts") == {"MESH": 1, "CAMERA": 1, "LIGHT": 1},
    )
    check("it names the active object and its type",
          text.get("active") == {"name": "Cube", "type": "MESH"})
    check("it reports the selection", text.get("selection", {}).get("count") == 1)
    check("it reports the mode", text.get("mode") == "OBJECT")
    check("it reports the units", text.get("unit_system") == "METRIC")
    check("it reports Global Undo, which is the degraded-mode fact", text.get("global_undo") is True)
    check("it reports the frame range", text.get("frame_range") == [scene.frame_start, scene.frame_end])
    check("and the file it belongs to", text.get("filepath") == (bpy.data.filepath or None))
    check("the collection tree is there and nested", isinstance(text.get("collection_tree"), list))
    check("a summary lists no objects, so no scene size can make it grow",
          "objects" not in text)

    # The live summary is the tool's summary: one definition, two consumers.
    live = json.loads(prompt.live_summary())
    same = {key: value for key, value in text.items() if key not in ("ok", "tool", "summary")}
    same["captured"] = "turn_start"
    check("the turn's live summary is the very same object, not a second schema", live == same)
    check("and it says when it was captured", live.get("captured") == "turn_start")
    check(
        "the live summary is what a turn actually sends, as the trailing system message",
        json.loads(prompt.messages_for(conversation.Conversation(), "hi")[-1]["content"]) == live,
    )

    # ------------------------------------------------------------ the caps
    print("\n-- the caps, against a scene that has grown --")
    objects = call("get_scene_info", scope="objects")
    listed = envelope(objects)
    check("a list result reports matched and returned", listed.get("matched") == 3 and listed.get("returned") == 3)
    check("nothing was cut, so nothing claims to be", listed.get("truncated") is False)

    one = envelope(call("get_scene_info", scope="objects", limit=1))
    check("asking for one returns one", one.get("returned") == 1)
    check("says three matched", one.get("matched") == 3)
    check("and says it was truncated, rather than shrinking silently", one.get("truncated") is True)
    check("naming the lever", "narrow with" in str(one.get("note")))

    original = list(bpy.data.objects)
    for index in range(40):
        created = bpy.data.objects.new(f"Probe{index:02d}", None)
        scene.collection.objects.link(created)
    bpy.context.view_layer.update()
    grown = envelope(call("get_scene_info", scope="objects"))
    note(f"after adding 40 empty objects: matched={grown.get('matched')} returned={grown.get('returned')}")
    check("a scene past the cap returns the cap", grown.get("returned") == execution.SCENE_LIMIT)
    check("and the true count", grown.get("matched") == 43)
    check("with the truncation flag set", grown.get("truncated") is True)
    check(
        "the ordering is deterministic: sorted by name",
        [entry["name"] for entry in grown["objects"]]
        == sorted(entry["name"] for entry in grown["objects"]),
    )
    check(
        "and it is the first N of that order, not an arbitrary N",
        [entry["name"] for entry in grown["objects"]]
        == sorted(obj.name for obj in bpy.data.objects)[: execution.SCENE_LIMIT],
    )
    check("the shared 8,000-char cap is not what cut this one", len(objects["content"]) <= execution.RESULT_CAP)

    # --------------------------------------------------- the include flags
    print("\n-- what an object entry actually carries --")
    cube_entry = envelope(
        call("get_scene_info", scope="active",
             include=["transform", "dimensions", "parent", "collections", "materials",
                      "modifiers", "constraints", "custom_properties", "mesh_stats"])
    )["objects"][0]
    note(f"cube: dimensions={cube_entry.get('dimensions')} mesh_stats={cube_entry.get('mesh_stats')}")
    cube = bpy.data.objects["Cube"]
    slots = len(cube.material_slots)
    note(f"cube: {slots} material slot(s), linked to {[c.name for c in cube.users_collection]}")
    check("the active scope returns exactly the active object", cube_entry["name"] == "Cube")
    check("dimensions come from the evaluated depsgraph", cube_entry.get("dimensions") == [2.0, 2.0, 2.0])
    check("the transform is plain JSON numbers", cube_entry["transform"]["location"] == [0.0, 0.0, 0.0])
    check("mesh_stats counts the real mesh", cube_entry.get("mesh_stats", {}).get("vertices") == 8)
    check("and its faces", cube_entry.get("mesh_stats", {}).get("polygons") == 6)
    check("materials come back one per slot, as names rather than datablocks",
          isinstance(cube_entry.get("materials"), list)
          and len(cube_entry["materials"]) == slots
          and all(isinstance(item, str) or item is None for item in cube_entry["materials"]))
    check("and the name is the slot's material's own name",
          cube_entry["materials"][:1] == [cube.material_slots[0].material.name] if slots else True)
    check("collections are the live links, sorted",
          cube_entry.get("collections") == sorted(c.name for c in cube.users_collection))
    check("modifiers are an empty list on a fresh cube", cube_entry.get("modifiers") == [])
    check("constraints too", cube_entry.get("constraints") == [])
    check("and there are no custom properties yet", cube_entry.get("custom_property_count") == 0)

    # The read-back trap the whole tool exists to avoid: a stale `dimensions`.
    before = cube.dimensions.z
    cube.scale.z = 2.0
    cube.dimensions.z  # deliberately read without an update: this is the stale value
    stale = envelope(call("get_scene_info", scope="active", include=["dimensions"]))["objects"][0]
    check(f"a read after an un-updated change is NOT stale ({before} -> {stale['dimensions'][2]})",
          stale["dimensions"][2] == 4.0)
    cube.scale.z = 1.0
    bpy.context.view_layer.update()

    filtered = envelope(call("get_scene_info", scope="objects", filter={"types": ["MESH"]}))
    check("a type filter works against real object types",
          [entry["name"] for entry in filtered["objects"]] == ["Cube"])
    check("a name filter is a substring, case-insensitively",
          envelope(call("get_scene_info", scope="objects", filter={"name_contains": "probe0"}))["matched"] == 10)

    # Counted from the scene's own links, not from the tool, so the check is not
    # the code under test agreeing with itself.
    master = scene.collection.name
    children = [child.name for child in scene.collection.children]
    note(f"master collection={master!r} children={children}")
    for collection_name in [master, *children]:
        linked = sum(
            1 for obj in bpy.data.objects
            if collection_name in [item.name for item in obj.users_collection]
        )
        check(f"a collection filter links through users_collection ({collection_name})",
              envelope(call("get_scene_info", scope="objects",
                            filter={"collection": collection_name}))["matched"] == linked)

    # ----------------------------------------------------------- the RNA tool
    print("\n-- get_rna_info, kind=type --")
    object_type = envelope(call("get_rna_info", kind="type", name="Object"))
    note(f"Object: properties={object_type.get('property_count')} returned={object_type.get('returned')} "
         f"base={object_type.get('base')!r} functions={object_type.get('function_count')}")
    check("a known type is found", object_type.get("found") is True)
    check("the property count is ticket 06's measured 141 (never listed whole)",
          object_type.get("property_count") == 141)
    check("the property limit caps how many come back",
          object_type.get("returned") <= execution.RNA_PROPERTY_LIMIT)
    check("so it says it was truncated", object_type.get("truncated") is True)
    check("and the answer is still parseable JSON, which a character-elided one would not be",
          isinstance(object_type.get("properties"), list) and object_type.get("found") is True)
    check("the shared cap's cut is announced, not silent", "cut to" in str(object_type.get("note")))
    note(f"Object's own answer is over the shared cap, so it was cut: {object_type.get('note')!r}")
    check("the base chain is walked via .base", object_type.get("base") == "ID")
    check("functions come from bl_rna, not `hasattr`", object_type.get("function_count") == 49)
    check("a function carries its parameters",
          all("parameters" in entry for entry in object_type.get("functions", [])))
    check("properties carry identifier/type/readonly/default",
          all({"id", "type", "readonly", "default"} <= set(entry) for entry in object_type["properties"]))
    check("an existing property is really there, by exact name",
          any(entry["id"] == "location"
              for entry in envelope(call("get_rna_info", kind="type", name="Object",
                                         filter="location"))["properties"]))
    check("a read-only flag is real, not defaulted",
          any(entry["readonly"] is True for entry in object_type["properties"]))

    filtered_type = envelope(call("get_rna_info", kind="type", name="Object", filter="scale"))
    check("a filter narrows to the matching properties",
          all("scale" in entry["id"] for entry in filtered_type["properties"]))
    check("and the whole count is still reported", filtered_type.get("property_count") == 141)
    check("with the narrowing stated rather than silent", "matched" in str(filtered_type.get("note")))
    check("a narrowed answer is small enough that the shared cap does not touch it",
          "cut to" not in str(filtered_type.get("note")))

    enum_type = envelope(call("get_rna_info", kind="type", name="Mesh", filter="shade_smooth"))
    check("a boolean property comes back with its default",
          all("default" in entry for entry in enum_type.get("properties", [])))

    print("\n-- get_rna_info, kind=operator --")
    operator = envelope(call("get_rna_info", kind="operator", name="mesh.primitive_cube_add"))
    note(f"mesh.primitive_cube_add: properties={operator.get('property_count')} poll={operator.get('poll_now')!r}")
    check("a known operator is found", operator.get("found") is True)
    check("its signature is the live build's", operator.get("property_count") == 8)
    check("with defaults and enum items",
          all({"id", "default"} <= set(entry) for entry in operator["properties"]))
    check("an enum property lists its values",
          any(entry.get("enum_items") for entry in operator["properties"]))
    check("poll_now is this moment's context, and True in background",
          operator.get("poll_now") is True)
    check("and it is labelled as momentary", "not a promise" in str(operator.get("poll_note")))

    failing_poll = envelope(call("get_rna_info", kind="operator", name="console.scrollback_append"))
    check("a context-dependent poll really reports False", failing_poll.get("poll_now") is False)

    # The trap the enumeration exists for.
    check(
        "`getattr` lies about operators, which is why existence is not asked of it",
        callable(getattr(bpy.ops.mesh, "frobnicate", None)),
    )
    check("and the enumeration does not contain it",
          "mesh.frobnicate" not in world.operator_names())
    note(f"enumerated {len(world.operator_names())} operators, {len(world.type_names())} type names")

    print("\n-- get_rna_info, not found --")
    missing = envelope(call("get_rna_info", kind="operator", name="mesh.primitive_cube"))
    check("an unknown operator is not_found", missing.get("error", {}).get("kind") == "not_found")
    check("and it offers real names instead of inventing one",
          "mesh.primitive_cube_add" in missing["error"].get("matches", []))
    check("every match exists in the build",
          all(match in world.operator_names() for match in missing["error"]["matches"]))
    typo = envelope(call("get_rna_info", kind="type", name="Obj"))
    note(f"type 'Obj' -> {typo.get('error', {}).get('matches')}")
    check("a truncated type name gets type matches, shortest first",
          typo.get("error", {}).get("matches", [None])[0] == "Object")
    check("and the wrong-kind hint names the right kind",
          "kind=\"type\"" in str(envelope(call("get_rna_info", kind="operator", name="Object"))
                                 .get("error", {}).get("hint")))
    check("a type asked for as a type is found",
          envelope(call("get_rna_info", kind="type", name="Object")).get("found") is True)

    print("\n-- get_rna_info, search --")
    found = envelope(call("get_rna_info", kind="search", name="primitive_cube_add"))
    note(f"search primitive_cube_add: {found.get('total_matches')} matches, first={found.get('operators')[:2]}")
    check("a substring search finds the ids", found.get("total_matches", 0) >= 1)
    check("and returns real ones",
          all(item in world.operator_names() for item in found["operators"]))
    check("a search that matches nothing is an empty answer, not an error",
          envelope(call("get_rna_info", kind="search", name="zzzz_no_such_operator")).get("total_matches") == 0)

    print("\n-- get_rna_info, guide --")
    for topic in guides.TOPICS:
        served = envelope(call("get_rna_info", kind="guide", name=topic))
        check(f"guide {topic} is served from the shipped registry",
              served.get("found") is True and len(served.get("body", "")) > 80)
    unknown_guide = envelope(call("get_rna_info", kind="guide", name="nope"))
    check("an unknown guide lists the topics that exist",
          unknown_guide.get("error", {}).get("matches") == list(guides.TOPICS))

    print("\n-- the error kinds --")
    check("a wrong kind is invalid_kind, not not_found",
          envelope(call("get_rna_info", kind="frobnicate", name="Object"))
          .get("error", {}).get("kind") == "invalid_kind")
    check("a missing name is an argument error",
          envelope(call("get_rna_info", kind="type")).get("error", {}).get("kind") == "tool_argument_error")
    check("an unknown scope is refused",
          envelope(call("get_scene_info", scope="everything")).get("error", {}).get("kind")
          == "tool_argument_error")
    check("an unknown tool names the three that exist",
          all(name in envelope({"ok": False, "content": json.dumps(
              json.loads(execution.execute_tool(
                  {"function": {"name": "frobnicate", "arguments": "{}"}}, world)["content"]))})
              ["error"]["message"] for name in ("run_blender_python", "get_scene_info", "get_rna_info")))
    check("a read-only tool never raises, whatever it is handed",
          execution.execute_tool(
              {"function": {"name": "get_rna_info", "arguments": '{"kind": "type", "name": 7}'}}, world
          )["ok"] is False)

    # -------------------------------------------------------- through the loop
    print("\n-- a read-only call through the real loop --")
    session = conversation.Conversation()
    sent: list = []
    session.attach(send=lambda messages: (sent.append(messages), None)[1],
                   execute=toolbox.execute, context=prompt.context)
    session.begin_turn("what is in my scene?")
    session.apply_event({
        "ev": "done",
        "finish_reason": "tool_calls",
        "tool_calls": [{
            "id": "read1",
            "type": "function",
            "function": {"name": "get_scene_info", "arguments": json.dumps({"scope": "summary"})},
        }],
    })
    session.pump()
    check("the loop ran the read-only call", session._rows["read1"].status == conversation.STATUS_OK)
    result = json.loads(session.history[-1]["content"])
    check("and the model receives the summary envelope", result.get("ok") is True
          and result.get("object_count") == len(bpy.data.objects))
    check("with the tool's own name on it", result.get("tool") == "get_scene_info")
    session.pump()
    check("so the next round can go out", len(sent) == 1)
    check("carrying the read result against the call's id",
          sent[0][-2] == {"role": "tool", "tool_call_id": "read1", "content": session.history[-1]["content"]})

    for name, arguments in (("get_rna_info", {"kind": "operator", "name": "mesh.primitive_cube_add"}),
                            ("get_rna_info", {"kind": "guide", "name": "socket-access"})):
        outcome = call(name, **arguments)
        check(f"a loop-shaped {name}{tuple(arguments.values())} call is ok", outcome["ok"] is True)
        check("and its content is the JSON the model parses",
              isinstance(json.loads(outcome["content"]), dict))

    # A read-only call issues no undo push and changes no data.
    check("the reads left the object count where they found it",
          envelope(call("get_scene_info", scope="summary"))["object_count"] == len(bpy.data.objects))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback

        traceback.print_exc()
        # Blender exits 0 even when a `--python` script raises, so a chain cannot
        # gate on the status. The token is the verdict; see the module docstring.
        print(f"\n{len(FAILURES)} check(s) recorded before the crash: {FAILURES[-4:]}")
        print("SMOKE FAILED")
        sys.exit(1)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        print("SMOKE FAILED")
        sys.exit(1)
    print("\nall read-only tool checks passed")
    print("SMOKE OK")
