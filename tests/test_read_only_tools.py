"""The two read-only tools, checked on plain CPython with no Blender.

    python3 tests/test_read_only_tools.py

Why this is its own file rather than a section of `tests/test_conversation.py`:
the checks below only touch `execution.py` and `guides.py`, both of which are
bpy-free and loadable by path, and separating them keeps them independent of the
loop's file. That turned out to matter: while build ticket 03 was being written,
`conversation.py` and `tests/test_conversation.py` were reverted to commit
`0225b64` by a concurrent writer, and the section that had been added to the
shared suite went with them. These checks survive that. They belong beside the
loop's checks once the tree is coherent again; the intent is one suite, not two.

They read through a **world**: the same duck-typed object `toolbox.LiveWorld`
implements against the real build, faked here with `SimpleNamespace`. So this file
proves everything the model receives - the caps, the flags, the filters, the
not-found matches, the shared result cap - and `tools/read_only_tools_probe.py`
proves that the real build is reachable through the same interface.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent


def load(name: str, path: Path):
    """Import a bpy-free module by path: the package itself imports `bpy`."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


execution = load("bc_execution_ro", ROOT / "blender_copilot" / "execution.py")
guides = load("bc_guides_ro", ROOT / "blender_copilot" / "guides.py")

FAILURES: list[str] = []


def check(label: str, condition) -> None:
    if condition:
        print(f"ok   {label}")
    else:
        FAILURES.append(label)
        print(f"FAILED {label}")


TOPICS = guides.TOPICS


class FakeObject:
    """An object with the attribute surface the readers touch."""

    def __init__(self, name, kind="MESH", custom=None, **fields):
        self.name = name
        self.type = kind
        self.location = (0.0, 0.0, 0.0)
        self.rotation_mode = "XYZ"
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self.dimensions = (2.0, 2.0, 2.0)
        self.parent = None
        self.users_collection = [SimpleNamespace(name="Collection")]
        self.material_slots = [SimpleNamespace(material=SimpleNamespace(name="Red"))]
        self.modifiers = [SimpleNamespace(name="Bevel", type="BEVEL")]
        self.constraints = []
        self.data = SimpleNamespace(vertices=range(8), edges=range(12), polygons=range(6))
        self._custom = dict(custom or {})
        for key, value in fields.items():
            setattr(self, key, value)

    def keys(self):
        return list(self._custom)

    def __getitem__(self, key):
        return self._custom[key]


def a_property(identifier, kind="FLOAT", **fields):
    values = {
        "identifier": identifier,
        "type": kind,
        "default": 1.5,
        "is_readonly": False,
        "array_length": 0,
        "enum_items": [],
        "hard_min": float("-inf"),
        "hard_max": float("inf"),
        "subtype": "",
        "description": f"{identifier} description",
    }
    values.update(fields)
    return SimpleNamespace(**values)


def an_rna(identifier, properties, functions=(), base=None, **fields):
    values = {
        "identifier": identifier,
        "name": identifier,
        "properties": list(properties),
        "functions": list(functions),
        "base": base,
        "description": f"the {identifier} type",
    }
    values.update(fields)
    return SimpleNamespace(**values)


# 60 properties, so the 40 default is exercised without a hand-written 141.
BLENDER_PROPS = [a_property(f"prop_{index:02d}") for index in range(60)]
BLENDER_PROPS.append(a_property("material_slots", type="COLLECTION", is_readonly=True))
BLENDER_PROPS.append(
    a_property("align", kind="ENUM", enum_items=[
        SimpleNamespace(identifier=f"ITEM_{index}") for index in range(40)
    ])
)
BLENDER_PROPS.append(a_property("scale_z", subtype="DISTANCE", hard_min=0.0, hard_max=100.0))
BLENDER_PROPS.append(a_property("rotation_mode", kind="ENUM", enum_items=[
    SimpleNamespace(identifier=n) for n in ("XYZ", "QUATERNION")
]))
OPERATOR_PROPS = [a_property(identifier) for identifier in ("size", "align", "enter_editmode")]
TYPE_RNA = an_rna("Object", BLENDER_PROPS, functions=[
    SimpleNamespace(identifier="select_set",
                    parameters=[SimpleNamespace(identifier="state", type="BOOLEAN")],
                    description="Select the object"),
    SimpleNamespace(identifier="material_slots_clear", parameters=[], description="Clear slots"),
])
OPERATOR_RNA = an_rna("mesh.primitive_cube_add", OPERATOR_PROPS)
# The menu name is the trap this fixture exists to carry: it sorts before `Object`
# and it is three times as long, and the real 5.2.2 build is full of such names.
TYPE_NAMES = ["ID", "Mesh", "OBJECT_MT_modifier_add", "Object", "Scene"]
OPERATOR_NAMES = ["mesh.primitive_cube_add", "mesh.primitive_cube_add_gizmo",
                  "object.select_all", "wm.save_mainfile"]

SCENE_FACTS = {
    "scene": "Scene",
    "filepath": "/tmp/factory.blend",
    "is_saved": True,
    "is_dirty": False,
    "mode": "OBJECT",
    "frame": 1,
    "frame_range": [1, 250],
    "render_engine": "BLENDER_EEVEE_NEXT",
    "unit_system": "METRIC",
    "global_undo": True,
}


class FakeWorld:
    """The scene and the RNA, with the shape `toolbox.LiveWorld` has."""

    def __init__(self, objects=(), active=None, selected=(), facts=None, collections=None):
        self._objects = list(objects)
        self._active = active
        self._selected = list(selected)
        self._facts = dict(facts if facts is not None else SCENE_FACTS)
        self._collections = collections if collections is not None else [
            {"name": "Collection", "count": len(self._objects), "children": []}
        ]

    def facts(self):
        return dict(self._facts)

    def objects(self):
        return list(self._objects)

    def active_name(self):
        return self._active

    def selected_names(self):
        return list(self._selected)

    def collections(self):
        return self._collections

    def bindings(self):
        return {}

    def type_names(self):
        return list(TYPE_NAMES)

    def operator_names(self):
        return list(OPERATOR_NAMES)

    def rna_of_type(self, name):
        return TYPE_RNA if name == "Object" else None

    def rna_of_operator(self, idname):
        return OPERATOR_RNA if idname == "mesh.primitive_cube_add" else None

    def operator_poll(self, idname):
        return idname == "mesh.primitive_cube_add"


CUBE = FakeObject("Cube", custom={"label": "hero", "tag": "x" * 400})
CAMERA = FakeObject("Camera", kind="CAMERA")
LIGHT = FakeObject("Light", kind="LIGHT", users_collection=[SimpleNamespace(name="Props")])
factory = FakeWorld([CUBE, CAMERA, LIGHT], active="Cube", selected=["Cube"],
                    collections=[{"name": "Collection", "count": 2, "children": [
                        {"name": "Props", "count": 1, "children": []}]}])


def scene_call(world, **arguments):
    call = {"id": "si", "function": {"name": execution.SCENE_INFO, "arguments": json.dumps(arguments)}}
    return execution.execute_tool(call, world)


def rna_call(world, **arguments):
    call = {"id": "ri", "function": {"name": execution.RNA_INFO, "arguments": json.dumps(arguments)}}
    return execution.execute_tool(call, world)


# ==========================================================================
# get_scene_info
# ==========================================================================
print("-- get_scene_info --")

summary = scene_call(factory)
check("a scene summary is ok", summary["ok"] is True)
check("it counts the objects", summary["envelope"]["object_count"] == 3)
check(
    "it counts them by type",
    summary["envelope"]["object_type_counts"] == {"MESH": 1, "CAMERA": 1, "LIGHT": 1},
)
check("it names the active object and its type",
      summary["envelope"]["active"] == {"name": "Cube", "type": "MESH"})
check("it reports the selection count and names",
      summary["envelope"]["selection"] == {"count": 1, "names": ["Cube"]})
check("it carries the scene facts", summary["envelope"]["render_engine"] == "BLENDER_EEVEE_NEXT")
check("and the degraded-mode fact", summary["envelope"]["global_undo"] is True)
check("the collection tree is nested, with counts",
      summary["envelope"]["collection_tree"][0]["children"][0]["count"] == 1)
check(
    "the summary scope of the field list is exactly ticket 06's",
    set(summary["envelope"]) - {"ok", "tool", "summary"}
    == {
        "scene", "filepath", "is_saved", "is_dirty", "mode", "frame", "frame_range",
        "render_engine", "unit_system", "global_undo", "object_count", "collection_tree",
        "collection_tree_truncated", "object_type_counts", "selection", "active",
        "captured", "schema",
    },
)
check("a summary is a summary: it lists nothing, whatever the scene holds",
      "objects" not in summary["envelope"])
check("the summary says when it was captured", summary["envelope"]["captured"] == "tool_call")
check(
    "and so does the turn's live summary, from the same definition",
    execution.scene_summary(factory, captured="turn_start")["captured"] == "turn_start"
    and set(execution.scene_summary(factory))
    == set(execution.scene_summary(factory, captured="tool_call")),
)

# The cap, and the flag that stops it reading as a small scene.
big_world = FakeWorld([FakeObject(f"Obj{index:03d}") for index in range(87)])
capped = scene_call(big_world, scope="objects")
check("a big scene is capped at the default", capped["envelope"]["returned"] == 25)
check("it reports how many matched", capped["envelope"]["matched"] == 87)
check("and says the list was cut", capped["envelope"]["truncated"] is True)
check(
    "the note names the narrowing lever, not just the number",
    capped["envelope"]["note"].startswith("87 matched, 25 returned; narrow with"),
)
check("the list really holds the cap", len(capped["envelope"]["objects"]) == 25)
check(
    "and it is deterministic: sorted by name",
    [entry["name"] for entry in capped["envelope"]["objects"]]
    == sorted(entry["name"] for entry in capped["envelope"]["objects"]),
)

wide = scene_call(big_world, scope="objects", limit=100)
check("the limit let everything through", wide["envelope"]["matched"] == 87)
check(
    "but 87 objects overflow the shared result cap, and that says so",
    wide["envelope"]["truncated"] is True and "cut to" in wide["envelope"]["note"],
)
check(
    "and `returned` is corrected to what actually came back",
    wide["envelope"]["returned"] == len(wide["envelope"]["objects"]) < 87,
)
check(
    "and the answer stays parseable JSON rather than being elided into prose",
    isinstance(json.loads(wide["content"])["objects"], list),
)
small = scene_call(factory, scope="objects")
check(
    "a list that fits both caps reports no truncation and no note",
    small["envelope"]["truncated"] is False and small["envelope"]["note"] is None,
)

# `include=[]` on purpose: entries are then just a name and a type, so the
# *list* cap is what is under test here rather than the shared 8,000-char cap.
asked_for_more = scene_call(FakeWorld([FakeObject(f"Obj{index:03d}") for index in range(130)]),
                            scope="objects", limit=500, include=[])
check("asking past the cap returns the cap", asked_for_more["envelope"]["returned"] == 100)
check("and says the cap was applied", "above the 100 maximum" in asked_for_more["envelope"]["note"])
check("the cap is reported as truncation too", asked_for_more["envelope"]["truncated"] is True)
check("a zero limit is raised rather than refused",
      scene_call(big_world, scope="objects", limit=0)["ok"] is True)
check("a numeric string limit is accepted",
      scene_call(big_world, scope="objects", limit="12")["envelope"]["returned"] == 12)
check("a nonsense limit falls back to the default and says so",
      "not a whole number" in scene_call(big_world, scope="objects", limit="lots")["envelope"]["note"])
check("an unknown scope is refused, naming the ones that exist",
      scene_call(factory, scope="everything")["envelope"]["error"]["kind"] == "tool_argument_error")

# Filters.
only_cameras = scene_call(factory, scope="objects", filter={"types": ["CAMERA"]})
check("a type filter narrows to the type",
      [entry["name"] for entry in only_cameras["envelope"]["objects"]] == ["Camera"])
check("the type filter is case-insensitive",
      scene_call(factory, scope="objects", filter={"types": ["mesh"]})["envelope"]["matched"] == 1)
check("a name filter is case-insensitive too",
      scene_call(factory, scope="objects", filter={"name_contains": "cub"})["envelope"]["matched"] == 1)
check("a collection filter uses the linked collections",
      scene_call(factory, scope="objects", filter={"collection": "Props"})["envelope"]["matched"] == 1)
check("a selection filter uses the selection",
      scene_call(factory, scope="objects", filter={"selected_only": True})["envelope"]["matched"] == 1)
check("the selection scope is the selection",
      scene_call(factory, scope="selection")["envelope"]["matched"] == 1)
check("a filtered-out list is not called truncated", only_cameras["envelope"]["truncated"] is False)
check(
    "an unknown filter field is refused, naming the field and the four that exist",
    "colour" in scene_call(factory, scope="objects", filter={"colour": "red"})["envelope"]["error"]["message"]
    and all(key in scene_call(factory, scope="objects", filter={"colour": "red"})["envelope"]["error"]["hint"]
            for key in execution.SCENE_FILTER_KEYS),
)
check(
    "a filter on a scope that does not take one is refused with a hint",
    scene_call(factory, scope="summary", filter={"types": ["MESH"]})["envelope"]["error"]["hint"],
)

# Includes.
deep = scene_call(factory, scope="active")
entry = deep["envelope"]["objects"][0]
check("the active scope lists the one object", len(deep["envelope"]["objects"]) == 1)
check("with its deep default include set",
      {"transform", "dimensions", "materials", "modifiers", "mesh_stats"} <= set(entry))
check("the transform is JSON numbers", entry["transform"]["location"] == [0.0, 0.0, 0.0])
check("mesh_stats counts the evaluated mesh", entry["mesh_stats"]["vertices"] == 8)
check("materials are named, not indexed", entry["materials"] == ["Red"])
check("modifiers carry name and type", entry["modifiers"] == [{"name": "Bevel", "type": "BEVEL"}])
check("a default-include list result carries only the transform",
      "dimensions" not in scene_call(factory, scope="objects")["envelope"]["objects"][0])
check(
    "mesh_stats is None for something that is not a mesh",
    scene_call(factory, scope="objects", filter={"types": ["CAMERA"]},
               include=["mesh_stats"])["envelope"]["objects"][0]["mesh_stats"] is None,
)
check(
    "an unknown include flag is refused, naming it and the flags that exist",
    "colour" in scene_call(factory, scope="objects", include=["colour"])["envelope"]["error"]["message"]
    and all(flag in scene_call(factory, scope="objects", include=["colour"])["envelope"]["error"]["hint"]
            for flag in execution.SCENE_INCLUDES),
)
props = scene_call(factory, scope="objects", filter={"name_contains": "Cube"},
                   include=["custom_properties"])["envelope"]["objects"][0]
check("custom properties come back stringified", props["custom_properties"]["label"] == "hero")
check("and capped at 200 characters", len(props["custom_properties"]["tag"]) <= 200)
check("with the true count alongside the capped dict", props["custom_property_count"] == 2)
check(
    "a read with no active object is a not_found, not an empty list",
    scene_call(FakeWorld([]), scope="active")["envelope"]["error"]["kind"] == "not_found",
)
check(
    "a call with no Blender in the room fails cleanly rather than raising",
    execution.execute_tool(
        {"function": {"name": execution.SCENE_INFO, "arguments": "{}"}}, None)["ok"] is False,
)

# The shared cap: a huge answer loses entries, and says so. It must stay parseable
# JSON, which is why the read-only tools drop entries rather than eliding
# characters: the model *parses* this answer.
huge = FakeWorld([FakeObject(f"Obj{index:03d}", custom={f"k{key}": "v" * 190 for key in range(10)})
                  for index in range(100)])
big_enough = scene_call(huge, scope="objects", limit=100,
                        include=["transform", "custom_properties", "modifiers"])
check("a result that would blow the shared cap drops entries instead",
      big_enough["envelope"]["truncated"] is True)
check("the wire content respects the cap", len(big_enough["content"]) <= execution.RESULT_CAP)
check("and it says which list was cut", "cut to" in str(big_enough["envelope"]["note"]))
check(
    "the returned count is corrected to what actually came back",
    big_enough["envelope"]["returned"] == len(big_enough["envelope"]["objects"]),
)
check("so the model can still parse its answer",
      json.loads(big_enough["content"])["matched"] == 100)
check("and the tool is still named on it",
      json.loads(big_enough["content"])["tool"] == execution.SCENE_INFO)


# ==========================================================================
# get_rna_info
# ==========================================================================
print("\n-- get_rna_info --")

object_type = rna_call(factory, kind="type", name="Object")
check("a known type is found", object_type["ok"] is True)
check("the whole property count is reported",
      object_type["envelope"]["property_count"] == len(BLENDER_PROPS))
check("only the limit is returned",
      object_type["envelope"]["returned"] == execution.RNA_PROPERTY_LIMIT)
check("and the cut is flagged", object_type["envelope"]["truncated"] is True)
check("with a note naming the lever", "narrow filter" in object_type["envelope"]["note"])
check("properties carry what ticket 06 asks for",
      {"id", "type", "readonly", "default"} <= set(object_type["envelope"]["properties"][0]))
check(
    "read-only is reported",
    rna_call(factory, kind="type", name="Object", filter="material_slots")
    ["envelope"]["properties"][0]["readonly"] is True,
)
check("functions come with their parameters",
      object_type["envelope"]["functions"][0]["parameters"][0]["id"] == "state")
check("and there are two of them", object_type["envelope"]["function_count"] == 2)
check("the base chain is walked, not guessed", object_type["envelope"]["base"] == "")

narrowed = rna_call(factory, kind="type", name="Object", filter="scale_z")
check("a filter narrows to the matching property", narrowed["envelope"]["returned"] == 1)
check("so nothing was truncated by the limit", narrowed["envelope"]["truncated"] is False)
check("but the narrowing is still stated",
      f"matched 1 of {len(BLENDER_PROPS)}" in narrowed["envelope"]["note"])
check("a numeric range travels with the property",
      narrowed["envelope"]["properties"][0]["range"] == [0.0, 100.0])
check("and so does the subtype", narrowed["envelope"]["properties"][0]["subtype"] == "DISTANCE")

enum_prop = rna_call(factory, kind="type", name="Object", filter="align")["envelope"]["properties"][0]
check("enum values are listed", len(enum_prop["enum_items"]) == execution.ENUM_ITEM_CAP)
check("and the true count always accompanies a capped list", enum_prop["enum_count"] == 40)

operator = rna_call(factory, kind="operator", name="mesh.primitive_cube_add")
check("a known operator is found", operator["ok"] is True)
check("it reports this moment's poll, explicitly as momentary",
      operator["envelope"]["poll_now"] is True and "not a promise" in operator["envelope"]["poll_note"])
check("with its properties and defaults",
      {entry["id"] for entry in operator["envelope"]["properties"]} == {"size", "align", "enter_editmode"})

missing_operator = rna_call(factory, kind="operator", name="mesh.primitive_cube")
check(
    "an unknown operator is not found",
    missing_operator["ok"] is False
    and missing_operator["envelope"]["error"]["kind"] == "not_found",
)
check("and it names close matches instead of inventing an answer",
      missing_operator["envelope"]["error"]["matches"][0] == "mesh.primitive_cube_add")
check("every match is a name that really exists",
      all(match in OPERATOR_NAMES for match in missing_operator["envelope"]["error"]["matches"]))
typo = rna_call(factory, kind="operator", name="object.select_al")
check("a typo also gets matches", "object.select_all" in typo["envelope"]["error"]["matches"])
check("a type asked for as an operator gets a hint naming the right kind",
      "kind=\"type\"" in rna_call(factory, kind="operator", name="Object")["envelope"]["error"]["hint"])
check("an unknown type gets matches from the type list",
      "Object" in rna_call(factory, kind="type", name="Objec")["envelope"]["error"]["matches"])
check(
    "and a short query is answered with the short name, not the menu ids that sort first",
    rna_call(factory, kind="type", name="Obj")["envelope"]["error"]["matches"][0] == "Object",
)

search = rna_call(factory, kind="search", name="cube")
check("a search finds every operator whose id contains the pattern",
      search["envelope"]["total_matches"] == 2)
check("and returns them sorted",
      search["envelope"]["operators"][0] == "mesh.primitive_cube_add")
check("a search that matches nothing is an empty answer, not an error",
      rna_call(factory, kind="search", name="frobnicate")["envelope"]["total_matches"] == 0)
capped_search = rna_call(factory, kind="search", name="e", limit=1)
check("a capped search says it was capped", capped_search["envelope"]["truncated"] is True)
check("with the lever named", "longer pattern" in capped_search["envelope"]["note"])

check(
    "a wrong kind is its own error kind, so a wrong question is not a missing name",
    rna_call(factory, kind="frobnicate", name="Object")["envelope"]["error"]["kind"] == "invalid_kind",
)
check(
    "and it lists the kinds that do exist",
    all(kind in rna_call(factory, kind="?", name="x")["envelope"]["error"]["message"]
        for kind in execution.RNA_KINDS),
)
check("a missing name is refused",
      rna_call(factory, kind="type")["envelope"]["error"]["kind"] == "tool_argument_error")

# The long tail, so an idiom question does not need a fourth tool.
for topic in TOPICS:
    guide_result = rna_call(factory, kind="guide", name=topic)
    check(f"guide {topic} is served",
          guide_result["ok"] is True and len(guide_result["envelope"]["body"]) > 80)
check("an unknown guide is not found", rna_call(factory, kind="guide", name="none")["ok"] is False)
check("and the matches are the topics that exist",
      rna_call(factory, kind="guide", name="none")["envelope"]["error"]["matches"] == list(TOPICS))
rna_description = execution.schema_for(execution.RNA_INFO)["function"]["description"]
check(
    "the description's topic list is generated from the registry, so it cannot drift",
    all(topic in rna_description for topic in TOPICS),
)
check("and it names all four kinds",
      all(kind in rna_description for kind in execution.RNA_KINDS))
check("the registry the checks walked is the shipped one",
      all(topic in guides.GUIDES for topic in TOPICS))

# The registry itself.
check("the three tools are declared, in the order the model reads them",
      execution.tool_names() == ["run_blender_python", "get_scene_info", "get_rna_info"])
check("every schema is an OpenAI tool schema",
      all(schema["type"] == "function" and schema["function"]["name"] for schema in execution.SCHEMAS))
check(
    "the read-only schemas require no mutating argument, so neither can be mistaken for a change",
    "purpose" not in json.dumps(execution.schema_for(execution.SCENE_INFO))
    and execution.schema_for(execution.RNA_INFO)["function"]["parameters"]["required"] == ["kind", "name"],
)
check(
    "an unknown tool is named, and the message names the three that exist",
    all(name in execution.execute_tool(
        {"function": {"name": "frobnicate", "arguments": "{}"}}, FakeWorld())["envelope"]["error"]["message"]
        for name in execution.tool_names()),
)

if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("\nall read-only tool checks passed")
