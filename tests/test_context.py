"""The projection, checked on plain CPython with no Blender.

    python3 tests/test_context.py

`context.py` imports no `bpy`, which is the point: the whole of *How a
conversation degrades as context grows* - the budget, the eviction order, the
head marker, the invariants - is decidable on a list of dicts, so it is decided
here rather than in a GUI session.

Three kinds of check, and the reason each exists:

  * **Named adversarial fixtures.** The cases ticket 14 §4 lists by name: an
    orphan `tool` result at the head, an assistant with calls and no prose, a
    result sitting exactly on the elision threshold, a current turn alone over
    budget, two rounds whose calls are the same size, and step 3 landing on
    `MIN_KEEP_TURNS`. A hand-picked case per way the order can be wrong.
  * **A seeded fuzz.** 300 generated conversations x 12 budgets, every projection
    run through `validate_projection`. A counterexample is a failing test, not an
    argument.
  * **Positive controls for the validator.** A validator that never fails proves
    nothing, so four deliberately broken projections are fed to it and each must
    be reported. This is what makes the fuzz's silence meaningful.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent


def load(name: str, path: Path):
    """Import a bpy-free module by path: the package itself imports `bpy`."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


context = load("context", ROOT / "blender_copilot" / "context.py")
transport = load("transport", ROOT / "blender_copilot" / "transport.py")
store_module = load("store", ROOT / "blender_copilot" / "store.py")

CHECKS = 0


def check(label: str, condition: bool, extra="") -> None:
    global CHECKS
    CHECKS += 1
    assert condition, f"FAILED: {label} {extra}"
    print(f"ok   {label}")


# ---------------------------------------------------------------- builders
def call(call_id: str, code: str = "pass", purpose: str = "do a thing") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "run_blender_python",
            "arguments": json.dumps({"code": code, "purpose": purpose}),
        },
    }


def result(call_id: str, size: int = 0, ok: bool = True) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps({"ok": ok, "stdout": "y" * size}),
    }


def exchange(turn: int, size: int = 100, prose: str = "prose") -> list[dict]:
    """One turn's middle: an assistant asking for one call, and its result."""
    call_id = f"t{turn}c0"
    asked = {"role": "assistant", "content": prose, "tool_calls": [call(call_id)]}
    return [asked, result(call_id, size)]


def turn(index: int, size: int = 100, tail: bool = True) -> list[dict]:
    messages = [{"role": "user", "content": f"turn {index} asks for something"}]
    messages.extend(exchange(index, size))
    if tail:
        messages.append({"role": "assistant", "content": f"turn {index} is done"})
    return messages


def store(turns: int, size: int = 100) -> list[dict]:
    history: list[dict] = []
    for index in range(turns):
        history.extend(turn(index, size))
    return history


def size_of(messages: list[dict]) -> int:
    return sum(context.message_bytes(message) for message in messages)


def settled_of(sent: list[dict], report: dict) -> list[dict]:
    """The projection's surviving settled messages, without the head marker."""
    start = 1 if report["has_marker"] else 0
    return sent[start : start + report["sent_settled"]]


def sent_bytes(sent: list[dict], report: dict) -> int:
    head = context.message_bytes(sent[0]) if report["has_marker"] else 0
    return head + size_of(settled_of(sent, report))


def exactly(size: int, call_id: str = "c0") -> dict:
    """A tool result whose *message* is exactly `size` bytes."""
    pad = 0
    while True:
        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "stdout": "y" * pad}),
        }
        current = context.message_bytes(message)
        if current >= size:
            assert current == size, (current, size)
            return message
        pad += size - current


# ---------------------------------------------------------------- the budget
check("the projection module never touches bpy", not re.search(
    r"^\s*(import bpy|from bpy)",
    (ROOT / "blender_copilot" / "context.py").read_text(encoding="utf-8"),
    re.M,
))
check(
    "the window is the provider's own, read from its docs (ticket 16)",
    context.WINDOW_TOKENS == 1_000_000,
)
check(
    "the output reserve is the ceiling this loop actually asks for, not a copied number",
    context.OUTPUT_RESERVE_TOKENS == transport.DEFAULT_MAX_OUTPUT_TOKENS,
)
check(
    "ticket 14 §1's arithmetic, on the 32k floor that budget was written against, "
    "reproduces the ratified 48,000",
    abs(context.derive_budget_bytes(32_000, 4_096) - 48_000) < 300,
    context.derive_budget_bytes(32_000, 4_096),
)
check(
    "the shipped default is that same arithmetic on the real 1M window",
    context.DEFAULT_HISTORY_BUDGET_BYTES == context.derive_budget_bytes(),
)
check(
    "sorted by the provider's real limit, the budget is the published floor revisited",
    context.DEFAULT_HISTORY_BUDGET_BYTES > 10 * 48_000,
    context.DEFAULT_HISTORY_BUDGET_BYTES,
)
check(
    "the preference is KiB, so the lever the design promised still exists",
    context.budget_from_kib(48) == 48 * 1024,
)
check(
    "a history that fits is sent byte-identical, with no marker and no edit",
    (lambda history: (lambda sent, report: sent == history and not report["trimmed"])(
        *context.plan(history, 10_000)
    ))(store(2)),
)

# ---------------------------------------------------------------- the order
big = 4_000
order_sizes = [4_000, 2_000, 1_000, 600]
history = []
for index, result_size in enumerate(order_sizes):
    history.extend(turn(index, result_size))
history.extend(turn(4, 100))
protected = len(history) - len(turn(4, 100))
largest = history[2]
# A budget the store misses by exactly the largest result's message: eliding it is
# enough, eliding any smaller one is not, so the answer shows which went first.
budget = (
    size_of(history[:protected])
    - context.message_bytes(largest)
    + context.MARKER_RESERVE_BYTES
    + 300
)
sent, report = context.plan(history, budget, current_turn_start=protected)
check("the projection is trimmed when the store does not fit", report["trimmed"])
check("the head marker is the first message", sent[0]["role"] == "system" and report["has_marker"])
check(
    "and it says what went, in ticket 14 §3's words",
    "elided to fit the context window" in sent[0]["content"]
    and "tool result" in sent[0]["content"],
    sent[0]["content"],
)
check(
    "elision stops as soon as it fits, so exactly one result was touched",
    report["tool_results_elided"] == 1,
    report,
)
check(
    "and it is the largest one: cost first, not age",
    report["elided"][0]["bytes_before"] == context.message_bytes(largest),
    report["elided"],
)
check(
    "every turn survives, because elision was enough on its own",
    report["turns_dropped"] == 0 and report["calls_dropped"] == 0,
)
check(
    "everything sent is inside the budget once the marker is counted",
    sent_bytes(sent, report) <= budget,
    (sent_bytes(sent, report), budget),
)
check("the store itself is untouched by planning", history[2] is largest)
check(
    "and the report names the turns that went, for the panel's expander",
    all(turn_["prompt"] and turn_["messages"] for turn_ in report["dropped"]),
    report["dropped"],
)

# When the budget is small enough that turns have to go, the tool output goes first
# and *everything* of the dropped kind goes with it - the steps are not
# alternatives, they are a ladder.
crowded = store(6, size=big)
sent, report = context.plan(crowded, 700, current_turn_start=len(crowded))
check("a budget that admits no turns first drops the call/result pairs", report["calls_dropped"] == 5, report)
check("and then takes whole turns, oldest first", report["turns_dropped"] >= 1, report)
check(
    "and the marker says how many turns and messages went by then",
    "earlier turn" in sent[0]["content"] and "messages" in sent[0]["content"],
    sent[0]["content"],
)
check(
    "a result that step 1 elided and step 2 then dropped is reported as dropped, "
    "never as something the model was shown",
    report["tool_results_elided"] == 0,
    report["elided"],
)

# Elision keeps the call, so the model still knows what each call was *for*.
check(
    "an elided result is a stub that names the tool and says to re-call it",
    all(
        json.loads(message["content"]).get("elided") is True
        and json.loads(message["content"]).get("note")
        for message in sent
        if message.get("role") == "tool" and "elided" in message.get("content", "")
    ),
)
check(
    "and the call it answers is still there, with its purpose",
    any(
        "do a thing" in (call_.get("function") or {}).get("arguments", "")
        for message in sent
        if message.get("role") == "assistant"
        for call_ in message.get("tool_calls") or []
    ),
)

def plain(index: int) -> list[dict]:
    """A turn with no tool calls: nothing for steps 1 and 2 to take."""
    return [
        {"role": "user", "content": f"turn {index} asks for something " * 6},
        {"role": "assistant", "content": f"turn {index} is done"},
    ]


# ---------------------------------------------------------------- the floor
check("the floor is two settled turns, as ratified", context.MIN_KEEP_TURNS == 2)
floor_history: list[dict] = []
for index in range(5):
    floor_history.extend(plain(index))
# A budget that fits two settled turns but not three: two turns have to go, and the
# rule stops there rather than eating another. The protected turn's own bytes are
# deliberately not in the budget.
budget = 850
sent, report = context.plan(floor_history, budget, current_turn_start=len(floor_history))
check("step 3 drops the oldest turns first", report["turns_dropped"] == 2, report["turns_dropped"])
check(
    "and stops at the floor instead of going lower",
    report["sent_settled"] == len(plain(2)) + len(plain(3)),
    report["sent_settled"],
)
check(
    "the newest settled turns are the ones kept",
    [
        message.get("content") for message in sent if message.get("role") == "user"
    ]
    == [plain(2)[0]["content"], plain(3)[0]["content"], plain(4)[0]["content"]],
)
check("which is still inside the budget", sent_bytes(sent, report) <= budget)

# The floor is a floor and then a cliff: the turns that still do not fit are dropped
# too, all the way to the amnesia case - never to a single lonesome turn, which would
# be the anaphora bug ticket 14 §2 step 3 exists to bound.
sent, report = context.plan(floor_history, 60, current_turn_start=len(floor_history))
check("below the floor the settled turns all go at once", report["amnesia"])
check(
    "so the projection is the marker and the protected turn",
    sent == [sent[0]] + plain(4),
    sent,
)
check("and it is honest that it does not fit", report["fits"] is False)

# ---------------------------------------------------------------- the in-flight turn
history = store(4, size=big)
protected = len(history) - len(turn(3, big))
sent, report = context.plan(history, 100, current_turn_start=protected)
check(
    "the in-flight turn is byte-identical to the store (I4)",
    sent[-len(history) + protected :] == history[protected:],
)
check("pinned there even when it alone blows the budget", report["amnesia"])
check("while everything settled is gone", report["turns_dropped"] == 3)
check(
    "the summary's slot is still the last message, so the caller appends nothing odd",
    sent[-1] == history[-1],
)

# ---------------------------------------------------------------- thresholds
def threshold_history(size: int) -> list[dict]:
    """One settled turn whose only result's *message* is exactly `size` bytes."""
    return [
        {"role": "user", "content": "turn 0 asks for something"},
        {"role": "assistant", "content": "prose", "tool_calls": [call("t0c0")]},
        exactly(size, "t0c0"),
    ] + turn(1, 100)


def threshold_budget(size: int) -> int:
    """Room for the store once its only result is elided - and not before.

    Derived from the measured sizes rather than typed in, so this is a threshold
    test rather than a coincidence: the stub's own size is rebuilt from the note the
    module publishes, and the caller checks that the store does *not* fit unelided.
    """
    settled = threshold_history(size)[:3]
    stub = context.message_bytes(
        {
            "role": "tool",
            "tool_call_id": "t0c0",
            "content": json.dumps(
                {
                    "ok": True,
                    "tool": "run_blender_python",
                    "elided": True,
                    "note": context.ELISION_NOTE,
                },
                ensure_ascii=False,
            ),
        }
    )
    after = size_of(settled) - context.message_bytes(settled[2]) + stub
    return after + context.MARKER_RESERVE_BYTES + 8


tiny = threshold_history(512)
check(
    "the threshold fixture really does not fit as it stands",
    size_of(tiny[:3]) > threshold_budget(512),
)
sent, report = context.plan(tiny, threshold_budget(512), current_turn_start=3)
check(
    "a result of exactly 512 bytes is not worth eliding",
    report["tool_results_elided"] == 0,
    report["elided"],
)
check("so the pair goes instead, which is the cheaper saving", report["calls_dropped"] == 1, report)
larger = threshold_history(513)
sent, report = context.plan(larger, threshold_budget(513), current_turn_start=3)
check(
    "513 bytes is, so the threshold is where ticket 14 put it",
    report["tool_results_elided"] == 1,
    report["elided"],
)
check("and eliding it is enough on its own", report["turns_dropped"] == 0 and report["fits"])

# ---------------------------------------------------------------- tie-break
tie: list[dict] = []
for index in range(2):
    tie.append({"role": "user", "content": f"turn {index} asks for something"})
    tie.extend(exchange(index, big))
tie.extend(turn(2, 100))
sent, report = context.plan(tie, 2_000, current_turn_start=6)
check(
    "two equally expensive results: the older one is elided first",
    report["tool_results_elided"] == 2
    and report["elided"][0]["index"] < report["elided"][1]["index"],
    report["elided"],
)
check(
    "and the older one is the one at index 1: the second message of the first turn",
    report["elided"][0]["index"] == 2,
    [(entry["index"], entry["bytes_before"]) for entry in report["elided"]],
)
again, _ = context.plan(tie, 2_000, current_turn_start=6)
check("the tie-break is deterministic", json.dumps(again) == json.dumps(sent))

# ---------------------------------------------------------------- orphans
orphan = [result("gone", 5_000)] + store(3, size=big)
sent, report = context.plan(orphan, 10_000)
check("a leading orphan result is dropped before anything else is considered", report["repaired"] == 1)
check(
    "so the projection has no result whose call is missing (I1)",
    context.validate_projection(orphan, sent) == [],
    context.validate_projection(orphan, sent),
)
duplicated_answer = store(1) + [result("t0c0", 10)]
duplicate_sent, duplicate_report = context.plan(duplicated_answer, 10_000)
check(
    "the same rule takes a duplicate answer, which would put one id twice on the wire",
    duplicate_report["repaired"] == 1,
)
check(
    "so no call id reaches the wire twice (I7)",
    context.validate_projection(duplicated_answer, duplicate_sent) == [],
    context.validate_projection(duplicated_answer, duplicate_sent),
)

# An assistant with calls and no prose that loses its calls must not be left as an
# empty message (I3).
silent = [
    {"role": "user", "content": "turn 0"},
    {"role": "assistant", "content": None, "tool_calls": [call("t0c0")]},
    result("t0c0", big),
] + turn(1, 100)
sent, report = context.plan(silent, 200, current_turn_start=3)
check("an assistant with no prose and no calls left is removed outright (I3)", all(
    message.get("role") != "assistant" or message.get("content") or message.get("tool_calls")
    for message in sent
))
check(
    "and the pair goes together, so nothing dangles",
    context.validate_projection(silent, sent, protected_from=3) == [],
    context.validate_projection(silent, sent, protected_from=3),
)

# ---------------------------------------------------------------- the validator
reference = store(3, size=big)
good = context.plan(reference, 60_000)[0]
check("a clean projection passes the validator", context.validate_projection(reference, good) == [])


def broken(name: str, messages: list[dict]) -> None:
    problems = context.validate_projection(reference, messages)
    check(f"the validator catches {name}", bool(problems), problems)


broken("a result whose call is missing (I1)", [message for message in good if message.get("role") != "tool"][:2] + [good[1]])
dangling = [dict(message) for message in good]
for message in dangling:
    if message.get("role") == "assistant" and message.get("tool_calls"):
        message["tool_calls"] = list(message["tool_calls"]) + [call("never-answered")]
        break
broken("a call with no result (I2)", dangling)
reordered = [dict(message) for message in good]
reordered.reverse()
broken("a projection in the wrong order (I5)", reordered)
duplicated = [dict(message) for message in good] + [dict(good[-1])]
broken("a duplicated message (I5/I7)", duplicated)

# ---------------------------------------------------------------- idempotence
history = store(4, size=big)
sent, report = context.plan(history, 3_000, current_turn_start=len(history))
survivors = sent[1:]
protected = len(survivors) - len(turn(3, big))
again, second = context.plan(survivors, 3_000, current_turn_start=protected)
check(
    "re-planning what the model saw loses nothing further (I10)",
    again == survivors,
    (len(again), len(survivors)),
)
check(
    "the marker is regenerated rather than carried: it describes what "
    "*this* call dropped, and this call dropped nothing",
    second["has_marker"] is False,
)
check(
    "so the loss is a fixed point, which is the part of I10 that can hold",
    context.plan(again, 3_000, current_turn_start=protected)[0] == again,
)

# ---------------------------------------------------------------- monotonicity
history = store(6, size=big)
counts = []
for budget in (500, 1_000, 2_000, 5_000, 10_000, 20_000, 40_000, 80_000, 160_000):
    projected, _ = context.plan(history, budget, current_turn_start=len(history))
    counts.append(len([message for message in projected if message.get("role") != "system"]))
check(
    "a larger budget never sends fewer stored messages (I10)",
    counts == sorted(counts),
    counts,
)

# ---------------------------------------------------------------- the fuzz
NAMES = ("run_blender_python", "get_scene_info", "get_rna_info")


def generated(rng: random.Random) -> list[dict]:
    history: list[dict] = []
    for turn_index in range(rng.randint(1, 12)):
        history.append(
            {"role": "user", "content": f"t{turn_index} " + "ask " * rng.randint(1, 8)}
        )
        for call_index in range(rng.randint(0, 4)):
            call_id = f"t{turn_index}c{call_index}"
            history.append(
                {
                    "role": "assistant",
                    # Sometimes there is no prose with the calls.
                    "content": ("said something " * rng.randint(1, 4)) or None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": rng.choice(NAMES),
                                "arguments": json.dumps(
                                    {"code": "x" * rng.randint(1, 300), "purpose": "p"}
                                ),
                            },
                        }
                    ],
                }
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "ok": rng.random() > 0.1,
                            "stdout": "y" * rng.randint(0, 8_000),
                        }
                    ),
                }
            )
        if rng.random() < 0.5:
            history.append({"role": "assistant", "content": "and that is done"})
    return history


rng = random.Random(20260926)
projections = 0
violations = 0
over_budget = 0
for case in range(300):
    history = generated(rng)
    spans = store_module.turn_spans(history)
    for _ in range(12):
        budget = rng.choice([1, 200, 600, 1_500, 4_000, 12_000, 40_000, 120_000, 400_000])
        protected = spans[-1][0] if spans and rng.random() < 0.5 else None
        sent, report = context.plan(history, budget, current_turn_start=protected)
        projections += 1
        problems = context.validate_projection(history, sent, protected_from=protected)
        if problems:
            violations += 1
            check(f"fuzz case {case} at {budget} produces a sound projection", False, problems)
        elif not report["amnesia"] and report["fits"] and sent_bytes(sent, report) > budget:
            over_budget += 1
            check(f"fuzz case {case} at {budget} stays inside its budget", False, report)

check(
    f"300 generated conversations x 12 budgets: {projections} projections, "
    "every one sound",
    violations == 0,
)
check(f"and none of them over its budget: {over_budget} exceptions", over_budget == 0)

# ------------------------------------------------- the context viewer's arithmetic
#
# The panel's context viewer shows these numbers, so the numbers are decided here -
# in the module that imports no `bpy`, which is the only place a test can argue with
# them. Three things are worth pinning: that the categories are a *partition* (they
# must sum to the total the header divides by, or the percentages lie), that each
# kind of byte lands in the category it claims to describe, and that `usage_summary`
# reports the provider's numbers without inventing any it did not send.

_schemas = [
    {
        "type": "function",
        "function": {
            "name": "run_blender_python",
            "description": "Run Python inside Blender.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
_conversation = [
    {"role": "user", "content": "make me a football of red and yellow"},
    {"role": "assistant", "content": "Building it.", "tool_calls": [{"id": "c1"}]},
    {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true, "stdout": ""}'},
]
_base = "You are Blender Copilot." * 40
_summary = "Scene: 1 cube."


def _wire(value) -> int:
    """The expected size, computed here rather than by the module under test."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


_view = context.usage(_conversation, base_prompt=_base, schemas=_schemas, summary=_summary)

check(
    "the viewer's categories are exactly the ones it reports sizes for",
    tuple(_view["sizes"]) == context.USAGE_CATEGORIES,
    list(_view["sizes"]),
)
check(
    "and every size is a non-negative integer",
    all(isinstance(size, int) and size >= 0 for size in _view["sizes"].values()),
    _view["sizes"],
)
check(
    "the header's total is the sum of the rows it draws",
    _view["total"] == sum(_view["sizes"].values()),
    (_view["total"], sum(_view["sizes"].values())),
)
check(
    "a `tool` message is a tool result, not prose",
    _view["sizes"]["Tool results"] == _wire(_conversation[2])
    and _view["sizes"]["Messages"]
    == _wire(_conversation[0]) + _wire(_conversation[1]),
    _view["sizes"],
)
check(
    "the base prompt, the schemas and the live summary each land in their own row",
    _view["sizes"]["System instructions"] == _wire({"role": "system", "content": _base})
    and _view["sizes"]["Tool definitions"] == _wire({"tools": _schemas})
    and _view["sizes"]["Scene summary"] == _wire({"role": "system", "content": _summary}),
    _view["sizes"],
)
check(
    "a bigger history means a bigger Messages row and nothing else moves",
    context.usage(
        _conversation + [{"role": "user", "content": "x" * 500}],
        base_prompt=_base,
        schemas=_schemas,
        summary=_summary,
    )["sizes"]
    == {
        **_view["sizes"],
        "Messages": _view["sizes"]["Messages"] + _wire({"role": "user", "content": "x" * 500}),
    },
)
check(
    "no tools declared costs nothing beyond the empty array the wire would carry",
    context.usage(_conversation) ["sizes"]["Tool definitions"] == _wire({"tools": []}),
    context.usage(_conversation)["sizes"]["Tool definitions"],
)
check(
    "the token figure is the byte figure divided by the stated ratio",
    _view["tokens"] == _view["total"] // context.BYTES_PER_TOKEN,
    (_view["tokens"], _view["total"]),
)
check(
    "the window is the one the budget is derived from",
    _view["window"] == context.WINDOW_TOKENS * context.BYTES_PER_TOKEN
    and _view["window_tokens"] == context.WINDOW_TOKENS,
    (_view["window"], _view["window_tokens"]),
)
check(
    "the reserved row is the output reserve the budget already subtracts",
    _view["reserved"] == context.OUTPUT_RESERVE_TOKENS * context.BYTES_PER_TOKEN
    and _view["reserved_tokens"] == context.OUTPUT_RESERVE_TOKENS,
    (_view["reserved"], _view["reserved_tokens"]),
)
check(
    "the fraction is the total against the window, and is never above 1",
    abs(_view["fraction"] - _view["total"] / _view["window"]) < 1e-12
    and context.usage([{"role": "user", "content": "x" * 10}], window_bytes=10)["fraction"] == 1.0,
    _view["fraction"],
)
check(
    "an empty request still costs its envelopes, and a zero window does not divide",
    context.usage([])["sizes"]
    == {
        "System instructions": _wire({"role": "system", "content": ""}),
        "Tool definitions": _wire({"tools": []}),
        "Messages": 0,
        "Tool results": 0,
        "Scene summary": _wire({"role": "system", "content": ""}),
    }
    and context.usage([{"role": "user", "content": "hi"}], window_bytes=0)["fraction"] == 0.0,
    context.usage([]),
)
check(
    "asking for no window means no window, not the derived one",
    context.usage([{"role": "user", "content": "x"}], window_bytes=100)["window"] == 100,
    context.usage([{"role": "user", "content": "x"}], window_bytes=100),
)
check(
    "nothing is claimed as measured until the provider says so",
    _view["measured"] is None,
    _view["measured"],
)

# `usage_summary`: the provider's own counts, read defensively. Each of these is a
# shape a provider could plausibly send, including the ones that must produce silence.
check(
    "the provider's counts are reported with its cache figure when it sends one",
    context.measured_line(
        {"prompt_tokens": 12345, "completion_tokens": 678, "prompt_cache_hit_tokens": 12000}
    )
    == "Measured by the provider: 12,345 in (12,000 cached), 678 out tokens.",
    context.measured_line(
        {"prompt_tokens": 12345, "completion_tokens": 678, "prompt_cache_hit_tokens": 12000}
    ),
)
check(
    "a provider that reports no cache field gets no cache claim",
    context.measured_line({"prompt_tokens": 10, "completion_tokens": 2})
    == "Measured by the provider: 10 in, 2 out tokens.",
    context.measured_line({"prompt_tokens": 10, "completion_tokens": 2}),
)
check(
    "no usage block, an empty one, or garbage: silence rather than a guess",
    context.measured_line(None) == ""
    and context.measured_line({}) == ""
    and context.measured_line({"prompt_tokens": "many"}) == ""
    and context.measured_line("nope") == "",
)
check(
    "a partial block reports the half it has",
    context.measured_line({"prompt_tokens": 7})
    == "Measured by the provider: 7 in tokens.",
    context.measured_line({"prompt_tokens": 7}),
)
# The calibration: with the request's byte size known, the line reports the *measured*
# bytes-per-token, which is the only way a reader can tell whether the panel's own
# estimate is close. 2026-09-26's live run made this worth printing: the provider
# counted 8,062 tokens where the estimate predicted far fewer.
_calibrated = context.measured_line(
    {"prompt_tokens": 2000, "completion_tokens": 10}, 8000
)
check(
    "with the request size known, the line reports the measured bytes per token",
    "4.0 bytes per token" in _calibrated and "estimates 3" in _calibrated,
    _calibrated,
)
check(
    "and without it, no ratio is claimed",
    "bytes per token" not in context.measured_line({"prompt_tokens": 2000})
    and "bytes per token" not in context.measured_line(
        {"prompt_tokens": 2000}, 0
    ),
)
check(
    "the request size is the messages plus the tool definitions",
    context.request_bytes([{"role": "user", "content": "hi"}], _schemas)
    == _wire({"role": "user", "content": "hi"}) + _wire({"tools": _schemas}),
    context.request_bytes([{"role": "user", "content": "hi"}], _schemas),
)
check(
    "and it agrees with the viewer's own total for the same request",
    # The two paths measure the same thing from opposite ends: `context.usage` is
    # handed the projection plus the prompt pieces the viewer knows about, while
    # `request_bytes` sums the messages the transport is actually about to send. The
    # live probe calibrates against the second, the panel draws the first, so if they
    # ever disagreed the viewer would be explaining a request nobody made.
    context.request_bytes(
        [
            {"role": "system", "content": _base},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": _summary},
        ],
        _schemas,
    )
    == context.usage(
        [{"role": "user", "content": "hi"}],
        base_prompt=_base,
        schemas=_schemas,
        summary=_summary,
        window_bytes=1,
    )["total"],
    (
        context.request_bytes([{"role": "user", "content": "hi"}], _schemas),
        context.usage([{"role": "user", "content": "hi"}], schemas=_schemas)["total"],
    ),
)

# The split line: the shares must sum to exactly 100, or a reader who adds them up
# finds a bug. The awkward totals are the point of the fixtures - a request of a few
# hundred bytes across five categories is where naive rounding drifts.
_split = context.share_line(_view["sizes"])
check(
    "the split line names every category and sums to exactly 100%",
    sum(context.shares(_view["sizes"]).values()) == 100
    and all(
        context.SHORT_LABELS[name] in _split for name in context.USAGE_CATEGORIES
    ),
    _split,
)
check(
    "the split line is short enough to wrap inside a sidebar",
    len(_split) < 120,
    (len(_split), _split),
)
check(
    "and it orders the shares by size, biggest first",
    (
        lambda shares: [shares[name] for name in context.USAGE_CATEGORIES]
        == sorted((shares[name] for name in context.USAGE_CATEGORIES), reverse=True)
    )(context.shares(_view["sizes"])),
    context.shares(_view["sizes"]),
)
_rng = random.Random(20260927)
_bad_shares = 0
for _ in range(500):
    _sizes = {
        name: _rng.choice([0, 1, 2, 7, 40, 300, 1_500, 90_000])
        for name in context.USAGE_CATEGORIES
    }
    if sum(context.shares(_sizes).values()) not in (0, 100):
        _bad_shares += 1
check(
    "500 random size mixes: every share set sums to 100, or to 0 for an empty request",
    _bad_shares == 0,
    _bad_shares,
)
check(
    "an all-zero request is all zeroes rather than a division by nothing",
    set(context.shares({name: 0 for name in context.USAGE_CATEGORIES}).values()) == {0}
    and "0%" in context.share_line({name: 0 for name in context.USAGE_CATEGORIES}),
)

print(f"\nall checks passed ({CHECKS})")
