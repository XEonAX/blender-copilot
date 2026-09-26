"""Checks for the conversation store. Plain CPython, no Blender.

    python3 tests/test_store.py

`store.py` imports nothing from `bpy` on purpose: the scope keying, the retention
cap and the file format are all decidable without a scene, and the only `bpy`
input they need - where the directory is - arrives as a string. Keeping it that
way is what lets these run at all, because `bpy.app.timers` and a real extension
directory both need Blender.

The wire messages below are built by `_turn()`, which is the only place the test
decides what a turn looks like. Every retention claim is checked against it rather
than against a hand-written expected list, so a change to the shape shows up in
the assertions instead of being masked by a stale fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "store", _HERE.parent / "blender_copilot" / "store.py"
)
assert _spec and _spec.loader
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)


def check(label: str, condition: bool) -> None:
    assert condition, f"FAILED: {label}"
    print(f"ok   {label}")


def _turn(number: int, calls: int = 1, size: int = 24) -> list[dict]:
    """One wire turn: a user message, then `calls` call/result pairs.

    `size` fattens the arguments and the result so a byte cap can be reached
    without hundreds of messages.
    """
    messages = [{"role": "user", "content": f"ask {number}"}]
    for index in range(calls):
        call_id = f"call_{number}_{index}"
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "run_blender_python",
                            "arguments": json.dumps(
                                {"code": "x" * size, "purpose": f"purpose {number}.{index}"}
                            ),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"ok": True, "output": "y" * size}),
            }
        )
    return messages


def _conversation(turns: int, calls: int = 1, size: int = 24) -> list[dict]:
    messages: list[dict] = []
    for number in range(1, turns + 1):
        messages.extend(_turn(number, calls=calls, size=size))
    return messages


# ---------------------------------------------------------------- turn spans
print("-- what a turn is --")

# A two-call turn is five messages (ask, call, result, call, result), so three of
# them span 15 and start at 0, 5 and 10.
spans = store.turn_spans(_conversation(3, calls=2))
check("three asks are three turns", len(spans) == 3)
check("a turn starts at the user's message", [span[0] for span in spans] == [0, 5, 10])
check("and consumes everything up to the next one", [span[1] for span in spans] == [5, 10, 15])
check(
    "the spans tile the history with no gap",
    all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))
    and spans[-1][1] == 15,
)
check(
    "a turn's messages are exactly its span",
    store.turn_spans([{"role": "user", "content": "hi"}]) == [(0, 1)],
)
check("an empty history has no turns", store.turn_spans([]) == [])
check(
    "a fragment before the first ask is its own span, not a silent drop",
    store.turn_spans([{"role": "tool", "tool_call_id": "ghost", "content": "{}"}]
                     + [{"role": "user", "content": "hi"}]) == [(0, 1), (1, 2)],
)


# ------------------------------------------------------------------- pruning
print("\n-- retention --")

# Five 3-message turns: 15 messages. A 6-message cap therefore keeps the last
# two turns and drops three, and the arithmetic is spelled out rather than
# computed so a change to `_turn` shows up here instead of hiding behind a
# recomputation.
full = _conversation(5)
kept, retention = store.prune(full, cap_messages=6)
check("the cap keeps the newest turns", kept == _conversation(5)[9:])
check("and counts what it dropped", retention["turns_dropped"] == 3)
check("naming the cap that bit", retention["reason"] == "cap_6_messages")
check("and stamping a cutoff", retention["dropped_through"].endswith("Z"))
check(
    "a pruned history still starts at a turn boundary",
    kept[0]["role"] == "user",
)
check(
    "no tool result lost the call that produced it",
    store.orphan_results(kept) == [] and store.unanswered(kept) == [],
)
check("nothing is dropped when the history fits", store.prune(full, cap_messages=200)[0] == full)

capped_bytes = store.prune(_conversation(4, size=1_000), cap_messages=10_000, cap_bytes=4_000)
check("a byte cap prunes too", len(capped_bytes[0]) < len(_conversation(4, size=400)))
check("and it also prunes whole turns", capped_bytes[0][0]["role"] == "user")
check(
    "the byte cap reports its own reason",
    capped_bytes[1]["reason"] == "cap_1MiB",
)
check(
    "whole turns means the tool invariant survives byte pruning",
    store.orphan_results(capped_bytes[0]) == [] and store.unanswered(capped_bytes[0]) == [],
)

one_huge = _conversation(3, size=20_000)
kept_one = store.prune(one_huge, cap_messages=10, cap_bytes=1_000)[0]
check(
    "the newest turn is kept even when it alone exceeds the cap",
    len(kept_one) == len(_turn(3, size=20_000)) and kept_one[0]["role"] == "user",
)
check(
    "pruning a single turn is a no-op, not an empty conversation",
    store.prune(_turn(1), cap_messages=1)[0] == _turn(1),
)

first, first_retention = store.prune(_conversation(6), cap_messages=6)
second, second_retention = store.prune(
    _conversation(6)[6:] + _conversation(2), cap_messages=6, previous=first_retention
)
check(
    "a second prune adds to the first rather than restarting the count",
    second_retention["turns_dropped"] > first_retention["turns_dropped"],
)
check(
    "the count is the sum",
    second_retention["turns_dropped"] == first_retention["turns_dropped"] + 4,
)

# An orphan is what the cap must never create. Both directions are checked,
# because "no orphan results" and "no dangling calls" are different bugs.
check(
    "an unanswered call is detected",
    store.unanswered(
        [{"role": "assistant", "content": None,
          "tool_calls": [{"id": "a1", "function": {"name": "t"}}]}]
    )
    == ["a1"],
)
check(
    "an orphaned result is detected",
    store.orphan_results([{"role": "tool", "tool_call_id": "a1", "content": "{}"}]) == ["a1"],
)


# ------------------------------------------------------------------ the store
print("\n-- the scope on disk --")

root = tempfile.mkdtemp(prefix="bc-store-")
# Named `conversations` on purpose: `delete_all` refuses to remove a tree that is
# not, and the real one is, so a test directory with another name would exercise
# the guard instead of the removal.
directory = os.path.join(root, store.CONVERSATIONS_DIR)
try:
    fresh = store.Store(directory)
    check("a store with a directory is available", fresh.available)
    check("an unavailable directory is available-false", not store.Store("").available)
    check(
        "a directory that cannot be created is available-false",
        not store.Store("/proc/definitely/not/writable").available,
    )

    scope, history = fresh.open("/tmp/scene-a.blend")
    check("a new scope starts empty", history == [] and scope.blend_path == "/tmp/scene-a.blend")
    check("and is not yet filed", scope.id == "" and not scope.pinned)
    check(
        "nothing is written until there is something to write",
        not os.path.exists(directory) or os.listdir(directory) == [],
    )

    scope.save(_conversation(2))
    files = sorted(os.listdir(directory))
    check("saving files the conversation", len(files) == 2 and store.INDEX_NAME in files)
    check("under a scope that is now pinned", scope.pinned and scope.id)
    check(
        "an id is content-free and file-safe",
        scope.id.isalnum() and "/" not in scope.id,
    )

    reopened, loaded = fresh.open("/tmp/scene-a.blend")
    check("reopening the same path finds the same conversation", reopened.id == scope.id)
    check("verbatim: the wire messages come back byte-identical", loaded == _conversation(2))
    check("and the scope's own label is the file", reopened.label == "scene-a.blend")

    other, other_history = fresh.open("/tmp/scene-b.blend")
    check("a different file gets a different conversation", other.id == "" and other_history == [])
    other.save(_turn(9))
    check("filed separately", fresh.open("/tmp/scene-a.blend")[1] == _conversation(2))
    check("with no bleed between them", fresh.open("/tmp/scene-b.blend")[1] == _turn(9))
    check(
        "and two conversations, two files",
        len([name for name in os.listdir(directory) if name != store.INDEX_NAME]) == 2,
    )

    # Save As: the same conversation, re-keyed, and reachable from the old name.
    moved, moved_history = fresh.open("/tmp/scene-b.blend")
    check("Save As adopts the new path", moved.adopt("/tmp/scene-c.blend") is True)
    check("keeping the old name as an alias", "/tmp/scene-b.blend" in moved.aliases)
    moved.save(moved_history)
    check(
        "the old path still finds the conversation, not a new one",
        fresh.open("/tmp/scene-b.blend")[1] == _turn(9),
    )
    check(
        "and so does the new one, with the same history",
        fresh.open("/tmp/scene-c.blend")[1] == _turn(9),
    )
    check("no duplicate was created", moved.id == fresh.open("/tmp/scene-c.blend")[0].id)
    check(
        "still one file, not two",
        len([name for name in os.listdir(directory) if name != store.INDEX_NAME]) == 2,
    )
    check(
        "adopting the same path again is not a change",
        moved.adopt("/tmp/scene-c.blend") is False,
    )

    # An unsaved file: session-only, said out loud, and nothing written.
    before = sorted(os.listdir(directory))
    unsaved, unsaved_history = fresh.open("")
    unsaved.save([{"role": "user", "content": "never written"}])
    check("an unsaved file keeps its conversation in memory", unsaved_history == [])
    check("is not pinned", not unsaved.pinned)
    check("and says so", unsaved.label == store.SESSION_ONLY_LABEL and "unsaved" in unsaved.label)
    check("writing it changes nothing on disk", sorted(os.listdir(directory)) == before)

    # Self-healing: the index is a convenience, the headers are the truth.
    index_path = os.path.join(directory, store.INDEX_NAME)
    index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    check("the index maps a path to an id", index["active"]["/tmp/scene-a.blend"] == scope.id)
    os.remove(index_path)
    check(
        "a missing index is rebuilt by scanning the headers",
        fresh.open("/tmp/scene-a.blend")[1] == _conversation(2),
    )
    Path(index_path).write_text("{not json", encoding="utf-8")
    check(
        "a corrupt index is rebuilt too",
        fresh.open("/tmp/scene-a.blend")[0].id == scope.id,
    )
    Path(index_path).write_text("{not json", encoding="utf-8")
    check("and a corrupt index cannot crash a scan", fresh.find("/tmp/nothing.blend") == "")

    # A corrupt conversation file is a lost conversation, not a crash.
    Path(os.path.join(directory, f"{scope.id}.json")).write_text("]]]", encoding="utf-8")
    check("a corrupt conversation file loads as nothing", fresh.load(scope.id) is None)
    check(
        "and a scope over it starts empty rather than raising",
        fresh.open("/tmp/scene-a.blend")[1] == [],
    )

    # `meta` is shared: this ticket writes `retention`, ticket 14 will write
    # `context_trim` into the same object, so neither may clobber the other.
    meta_scope, meta_history = fresh.open("/tmp/scene-d.blend")
    meta_scope.save(meta_history + _turn(1))
    path_d = os.path.join(directory, f"{meta_scope.id}.json")
    record = json.loads(Path(path_d).read_text(encoding="utf-8"))
    record["meta"]["context_trim"] = {"turns_elided_total": 7}
    Path(path_d).write_text(json.dumps(record), encoding="utf-8")
    again, again_history = fresh.open("/tmp/scene-d.blend")
    again.save(again_history + _turn(2))
    kept_record = json.loads(Path(path_d).read_text(encoding="utf-8"))
    check(
        "another ticket's meta survives this ticket's write",
        kept_record["meta"]["context_trim"] == {"turns_elided_total": 7},
    )
    check(
        "and this ticket's own meta is there",
        "retention" in kept_record["meta"] and kept_record["schema"] == store.SCHEMA,
    )
    check("the wire messages are stored under `messages`", kept_record["messages"] == again_history + _turn(2))
    check("a stored record names its scope", kept_record["scope"]["blend_path"] == "/tmp/scene-d.blend")
    check("and is stamped", kept_record["updated"].endswith("Z"))

    # The directory that cannot be reached degrades instead of raising.
    unbacked = store.Store(lambda: "")
    degraded, degraded_history = unbacked.open("/tmp/scene-e.blend")
    degraded.save(_turn(1))
    check("an unavailable store keeps the conversation in memory", degraded_history == [])
    check("does not claim to be pinned", not degraded.pinned)
    check("and says why in a label", "session only" in degraded.label)

    # A symlink named `conversations` pointing somewhere real: the one shape that
    # would make "delete the conversations folder" delete something else.
    elsewhere = os.path.join(root, "elsewhere")
    os.makedirs(elsewhere)
    Path(os.path.join(elsewhere, "keep.json")).write_text("{}", encoding="utf-8")
    link = os.path.join(root, "linked", store.CONVERSATIONS_DIR)
    os.makedirs(os.path.dirname(link))
    os.symlink(elsewhere, link)
    check("delete all refuses a symlinked conversations directory", store.Store(link).delete_all() == 0)
    check("and the directory it pointed at is untouched", os.path.exists(os.path.join(elsewhere, "keep.json")))

    # Delete all: explicit, and nowhere near anything but its own directory.
    removed = fresh.delete_all()
    check("delete all removes the conversations", removed >= 3)
    check("including the index", not os.path.exists(index_path))
    check("and starts the session over", fresh.find("/tmp/scene-a.blend") == "")
    check("nothing outside the conversations directory is touched", os.path.isdir(root))
finally:
    shutil.rmtree(root, ignore_errors=True)

# The guard on `delete_all` is not paranoia: the path comes from Blender's own
# API and this is the one call that removes a tree.
check(
    "delete all refuses anything that is not a conversations directory",
    store.Store("/tmp").delete_all() == 0,
)
check(
    "and refuses to guess when there is no directory at all",
    store.Store("").delete_all() == 0,
)
check("a long basename is shortened at the front", store.scope_label("/x/" + "n" * 60 + ".blend").startswith("\u2026"))
check("a short one is shown whole", store.scope_label("/a/b/scene.blend") == "scene.blend")

print("\nall checks passed")
