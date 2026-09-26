"""What the model sees when the conversation outgrows the window.

*How a conversation degrades as context grows* is the decision - ratified
2026-09-25 - and this module is that decision built rather than re-argued. It
holds the whole projection: how much history may be sent, what gives way first,
the head marker that records a gap, and the report the panel turns into a note.

Three properties are load-bearing, and none of them is decoration:

  * **It is a projection, never a write.** `plan()` reads a list of wire messages
    and returns a new one plus a report. It imports no `bpy`, touches no file, and
    the stored conversation is the user's record: what the model no longer sees is
    still on disk, and re-running `plan()` recomputes the same answer. That is why
    "the stored transcript is unchanged by trimming" is a property of the shape
    rather than a promise about behaviour.
  * **It is measured in UTF-8 bytes of the wire JSON.** There is no tokeniser
    anywhere in Blender's bundle (checked against the installed build's own
    interpreter, ticket 14 §1), so bytes stand in for tokens at a planning divisor
    of 3 - an overestimate for English prose, which errs toward trimming early,
    and close to exact for the scripts that break a chars-per-token rule.
  * **The budget comes from the provider's real window.** The design was written
    against an assumed 32k floor and ratified at 48,000 bytes; the same day the
    owner annotated that premise as far too conservative, and *The first live send
    against DeepSeek* read the real figure off the provider's own docs. So
    `derive_budget_bytes()` is ticket 14 §1's arithmetic - the same reserve stack,
    the same divisor - with `WINDOW_TOKENS` set to that reading, and
    `tests/test_context.py` checks that handing it the old floor reproduces the
    ratified 48,000. The number is derived; the formula was not invented here.

**The eviction order is cost x reconstructibility, not age** (ticket 14 §2), and
the reason is worth keeping next to the code that implements it:

  1. **elide tool results** - largest first, then oldest. It is the cheapest thing
     to lose: the tool is still there, the model can re-call it, and a stale
     enumeration of a scene that has since changed is not merely old, it is
     *false*.
  2. **drop call/result pairs** - oldest turn first, oldest call first. The
     assistant's prose stays, which is the part a reader needs.
  3. **drop whole settled turns** - oldest first, never below `MIN_KEEP_TURNS`.
  4. **the amnesia floor** - every settled turn goes, and the turn in flight is
     sent alone.

Age alone would evict a one-line user constraint from twenty turns ago ("use
metres") while keeping a three-turn-old 8,000-byte scene dump, because a turn's
head is its user message and its tail is the tool noise. Degrading by kind
decouples them, so intents outlive machine output.

Nothing is ever summarised by the model. The live scene summary is regenerated per
request and is fresher than any narrative could be, and a summary would have to be
written at exactly the moment the context is too full to afford one (ticket 14 §3).
The gap is recorded instead: a mechanical head marker inside the request, and a
note in the panel, never silence.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _sibling(name: str):
    """Import a bpy-free sibling module, from a package or from this file's folder.

    The same problem `execution._sibling` solves: the CPython suite, the probes and
    the draw smoke all load these modules by path, with no package around them, so
    a plain `from . import store` is not always available.
    """
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception:  # noqa: BLE001 - any failure here means "load it by path"
        path = Path(__file__).with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"bc_{name}", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {name} from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


store = _sibling("store")


# ---------------------------------------------------------------------------
# The budget
#
# Ticket 14 §1's arithmetic, with each reserve named for what it is:
#
#   window bytes - prompt - live summary - reserved output - room for the turn
#     in flight = the bytes the prunable history may occupy
#
# The last term is not a number ticket 14 invented either: it is the difference
# between its own 96 KB window and its ratified 48,000-byte budget, and the check
# in `tests/test_context.py` re-derives 48,000 from the assumed 32k floor to prove
# this function is that sum rather than a replacement for it.
# ---------------------------------------------------------------------------

# DeepSeek's documented context window (ticket 16, read live from
# `api-docs.deepseek.com` on 2026-09-25). Not introspectable: the model string is
# free text (ticket 05 §4) and no API here reports a window, so this is a
# transcription of a published fact and is the one constant a new backend changes.
WINDOW_TOKENS = 1_000_000

# ASCII English is about 4.2 bytes/token, code and JSON about 3, CJK about 3. The
# divisor is a planning constant and not a measurement - there is nothing to
# measure it against in the bundle - so it is chosen to overestimate for prose,
# which trims slightly early and is the safe direction to be wrong in.
BYTES_PER_TOKEN = 3

# The floor the ratified budget was sized against, kept so the derivation stays
# checkable against the decision it came from.
ASSUMED_FLOOR_TOKENS = 32_000
RATIFIED_BUDGET_BYTES = 48_000

PROMPT_RESERVE_BYTES = 4_000
SUMMARY_RESERVE_BYTES = 1_500
CURRENT_TURN_RESERVE_BYTES = 30_000


def _output_reserve_tokens() -> int:
    """What one round asks the provider for, from the module that asks for it.

    Read rather than copied: `transport.DEFAULT_MAX_OUTPUT_TOKENS` is the ceiling
    `transport.config()` hands a real request (per model, from the model's own
    documented maximum), and a budget derived from a number that only looked right
    once is how the reserves would silently stop covering the reply.
    """
    try:
        return int(_sibling("transport").DEFAULT_MAX_OUTPUT_TOKENS)
    except Exception:  # noqa: BLE001 - the derivation must not depend on an import
        return 384_000


OUTPUT_RESERVE_TOKENS = _output_reserve_tokens()


def derive_budget_bytes(window_tokens: int = WINDOW_TOKENS, output_tokens: int | None = None) -> int:
    """Bytes of prunable history a window of `window_tokens` can afford.

    Pure arithmetic over the reserve stack above, so the answer for any window can
    be recomputed on the spot - which is what makes "the budget is derived from the
    provider's real limit" a statement about code rather than about a comment.
    """
    if output_tokens is None:
        output_tokens = OUTPUT_RESERVE_TOKENS
    window = int(window_tokens) * BYTES_PER_TOKEN
    reserves = (
        PROMPT_RESERVE_BYTES
        + SUMMARY_RESERVE_BYTES
        + int(output_tokens) * BYTES_PER_TOKEN
        + CURRENT_TURN_RESERVE_BYTES
    )
    return max(0, window - reserves)


# The shipped default: the same arithmetic on the real window instead of the
# assumed one. Roughly 2.8 MiB, which is far more history than the store's own
# 1 MiB cap will ever hold - so in practice the store binds first and this budget
# only bites on a turn that is enormous on its own (retention never drops the
# newest turn), or when a user lowers the preference to fit a smaller model.
DEFAULT_HISTORY_BUDGET_BYTES = derive_budget_bytes()

# The addon preference's range (*How a conversation degrades as context grows* §1:
# 8-512 KiB). It is a lever the user pulls *down*, which is the safe direction on
# a backend whose window is known: the default already sits above its ceiling, and
# the ceiling is what a user with a smaller model needs.
MIN_BUDGET_KIB = 8
MAX_BUDGET_KIB = 512


def budget_from_kib(kib: int) -> int:
    """The addon's KiB preference as bytes."""
    return int(kib) * 1024


# ---------------------------------------------------------------------------
# The constants of the order
# ---------------------------------------------------------------------------

# Ticket 14 §2 step 1 only elides a result whose bytes exceed this: a stub that
# saves nothing is pure loss, and the stub itself is about 230 bytes.
ELIDE_FLOOR_BYTES = 512

# Ticket 14 §2 step 3's floor: below this the anaphora in the remaining turns
# breaks, and step 4 is the honest end of the line rather than a quieter one.
MIN_KEEP_TURNS = 2

# The head marker's message is ~165 bytes. Reserving a little more than that lets
# the eviction loop charge for a marker it has not built yet - the counts in it
# are only known when the eviction stops.
MARKER_RESERVE_BYTES = 256

ELISION_NOTE = "result elided to fit the context window; re-call the tool if you need it"

# The sentence the panel's expander shows, and the reason the order is what it is.
EVICTION_ORDER = (
    "largest tool output first, then whole calls, then whole turns - "
    "what is cheap to reconstruct goes first, not what is oldest"
)

NOTE_PREFIX = "\u2702 context trimmed"


def message_bytes(message: dict) -> int:
    """One message's cost: UTF-8 bytes of its wire JSON, compact.

    The ratified unit (ticket 14 §1). `store.encoded_size` measures a whole
    history with `json.dumps`'s default separators, which is a few percent larger
    and is the *store's* number; both are bytes of the same JSON, and the two
    tickets chose their own whitespace.
    """
    return len(
        json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def _envelope(content):
    """A tool result's envelope when it parses, else None."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _repair(history: list[dict]) -> tuple[list[dict], set]:
    """I6/I7: drop what a hand-edited file or an interrupted prune can leave.

    Two shapes, neither of which the store's own write path can produce - the loop
    answers every call it has asked for, and `store.prune` cuts at turn
    boundaries: a `tool` result whose call is not earlier in the same history (or
    that answers a call a second time, which would put one id on the wire twice),
    and anything before the first `user` message, which has no turn to belong to.

    Indices into the *original* list come back with the survivors so the caller's
    `current_turn_start` can be shifted past whatever was dropped here.
    """
    kept: list[tuple[int, dict]] = []
    dropped: set[int] = set()
    calls_seen: set = set()
    answered: set = set()
    for index, message in enumerate(history):
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in calls_seen or call_id in answered:
                dropped.add(index)
                continue
            answered.add(call_id)
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id:
                    calls_seen.add(call_id)
        kept.append((index, dict(message)))

    first_turn = next(
        (
            position
            for position, (_, message) in enumerate(kept)
            if message.get("role") == "user"
        ),
        None,
    )
    if first_turn:
        dropped.update(index for index, _ in kept[:first_turn])
        kept = kept[first_turn:]
    return [message for _, message in kept], dropped


def _protected_start(history: list[dict], current_turn_start, repaired: set) -> int:
    """Where the messages that must survive byte-identically begin.

    `current_turn_start` is the caller's index of the in-flight turn's first
    message (invariant I4: from the user's message through its last tool result
    nothing is edited, dropped, or re-ordered). `None` - or an index that no
    longer names a message, which is what a caller with no turn in flight has -
    means the newest turn is protected instead: it is the one the user just had,
    and `store.prune` never drops it either.
    """
    if not history:
        return 0
    index = None
    if current_turn_start is not None:
        index = int(current_turn_start) - len(
            [value for value in repaired if value < int(current_turn_start)]
        )
    if index is None or not 0 <= index < len(history):
        spans = store.turn_spans(history)
        return spans[-1][0] if spans else 0
    while index > 0 and history[index].get("role") != "user":
        index -= 1
    return index


def _elided_result(message: dict, tool: str) -> dict:
    """The stub step 1 leaves behind (ticket 14 §2 step 1's shape).

    The message, its role and its `tool_call_id` survive; the output does not.
    `ok` is carried over when the result's own envelope can be parsed and is
    `null` when it cannot, because inventing `false` would assert a failure that
    nobody observed.
    """
    envelope = _envelope(message.get("content"))
    ok = envelope.get("ok") if isinstance(envelope, dict) else None
    if not isinstance(ok, bool):
        ok = None
    content = json.dumps(
        {"ok": ok, "tool": tool, "elided": True, "note": ELISION_NOTE},
        ensure_ascii=False,
    )
    return {"role": "tool", "tool_call_id": message.get("tool_call_id"), "content": content}


def _tool_name(messages: list[dict], call_id) -> str:
    """The tool a call id asked for, or "" when no call in the list claims it."""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("id") == call_id:
                return str((call.get("function") or {}).get("name") or "")
    return ""


def _marker_text(turns: int, messages: int, results: int = 0) -> str:
    """Ticket 14 §3's mechanical line, with counts that only ever appear if real.

    An elision-only trim has no turn to report, and a marker that said "earlier
    material" there would be technically true and useless to the model reading it -
    the point of the line is to tell it what it can no longer see, so it names the
    tool results instead.
    """
    parts = []
    if turns:
        parts.append(f"{turns} earlier turn" + ("" if turns == 1 else "s"))
    if messages:
        parts.append(f"{messages} message" + ("" if messages == 1 else "s"))
    if results:
        parts.append(f"{results} tool result" + ("" if results == 1 else "s"))
    subject = ", ".join(parts) or "earlier material"
    return (
        f"[{subject} elided to fit the context window; the live scene summary is "
        "current, and tools can be re-called.]"
    )


def plan(
    history: list[dict],
    budget_bytes: int,
    current_turn_start: int | None = None,
) -> tuple[list[dict], dict]:
    """The projection: `(sent, report)`.

    `sent` is what replaces `history` in the request - the head marker when
    anything went, then the surviving messages in store order - and `report` is
    everything the caller needs to say what happened. The caller adds the base
    prompt before it and the live summary after it; this function neither knows
    nor cares that they exist.

    Pure: `history` is never mutated, and the messages that come back are copies.
    """
    stored, repaired = _repair(history)
    protected = _protected_start(stored, current_turn_start, repaired)
    spans = store.turn_spans(stored)
    settled_spans = [span for span in spans if span[0] < protected]

    slots = [dict(message) for message in stored]
    alive = [True] * len(slots)
    elided: list[dict] = []
    calls_dropped = 0

    def settled_bytes() -> int:
        return sum(
            message_bytes(slots[index]) for index in range(protected) if alive[index]
        )

    def degraded() -> bool:
        """Whether anything the model would have seen has already gone."""
        return (
            bool(elided)
            or calls_dropped > 0
            or any(not alive[index] for index in range(protected))
        )

    def fits() -> bool:
        # The head marker is charged to the same budget: it is part of what goes
        # out and is not in the store, so forgetting it would let a projection that
        # "fits" be larger than the budget by the marker it added.
        return settled_bytes() + (MARKER_RESERVE_BYTES if degraded() else 0) <= budget_bytes

    # -- step 1: tool results, largest first, then oldest --------------------
    candidates = sorted(
        (
            (-message_bytes(slots[index]), index)
            for index in range(protected)
            if alive[index] and slots[index].get("role") == "tool"
            and message_bytes(slots[index]) > ELIDE_FLOOR_BYTES
        )
    )
    for _, index in candidates:
        if fits():
            break
        before = message_bytes(slots[index])
        replacement = _elided_result(
            slots[index], _tool_name(slots, slots[index].get("tool_call_id"))
        )
        saved = before - message_bytes(replacement)
        if saved <= 0:
            continue
        slots[index] = replacement
        elided.append(
            {
                "index": index,
                "tool": _tool_name(slots, slots[index].get("tool_call_id")),
                "bytes_before": before,
                "bytes_after": message_bytes(replacement),
                "saved": saved,
            }
        )

    # -- step 2: call/result pairs, oldest turn first, oldest call first -----
    if not fits():
        pairs: list[tuple[int, dict]] = []
        for start, end in settled_spans:
            for index in range(start, end):
                message = slots[index]
                if message.get("role") != "assistant":
                    continue
                for call_ in message.get("tool_calls") or []:
                    pairs.append((index, call_))
        for parent, call_ in pairs:
            if fits():
                break
            call_id = call_.get("id")
            result_index = next(
                (
                    index
                    for index in range(protected)
                    if alive[index]
                    and slots[index].get("role") == "tool"
                    and slots[index].get("tool_call_id") == call_id
                ),
                None,
            )
            remaining = [
                entry
                for entry in (slots[parent].get("tool_calls") or [])
                if entry.get("id") != call_id
            ]
            slots[parent] = dict(slots[parent])
            if remaining:
                slots[parent]["tool_calls"] = remaining
            else:
                slots[parent].pop("tool_calls", None)
                # I3: an assistant with nothing left but an empty string is not a
                # message any provider will take, and it is not one a reader wants.
                if not str(slots[parent].get("content") or "").strip():
                    alive[parent] = False
            if result_index is not None:
                alive[result_index] = False
            calls_dropped += 1

    # -- step 3: whole settled turns, oldest first, never below the floor ----
    if not fits():
        floor = min(MIN_KEEP_TURNS, len(settled_spans))
        for position in range(0, len(settled_spans) - floor):
            if fits():
                break
            start, end = settled_spans[position]
            for index in range(start, min(end, protected)):
                alive[index] = False

    # -- step 4: the amnesia floor ------------------------------------------
    amnesia = False
    if not fits():
        for index in range(protected):
            alive[index] = False
        amnesia = True

    # -- the report, derived from what survives rather than accumulated ------
    #
    # Every count below is read off the two lists at the end rather than tallied
    # while evicting: a message that step 2 emptied and step 3 then dropped would
    # otherwise be counted twice, and a report that overstates how much was lost is
    # the one lie this design cannot afford.
    dropped_turns = []
    for number, (start, end) in enumerate(settled_spans, start=1):
        survivors = [index for index in range(start, min(end, protected)) if alive[index]]
        if survivors:
            continue
        span = stored[start:end]
        prompt = next(
            (
                " ".join(str(message.get("content")).split())[:60]
                for message in span
                if message.get("role") == "user" and message.get("content")
            ),
            "(no user message)",
        )
        dropped_turns.append({"turn": number, "prompt": prompt, "messages": len(span)})

    settled_before = sum(message_bytes(message) for message in stored[:protected])
    settled_sent = sum(1 for index in range(protected) if alive[index])
    messages_dropped = protected - settled_sent
    # Only the stubs that reach the wire are reported as elisions. Step 1 can elide a
    # result that step 2 then drops outright, and counting that as both would make the
    # note claim the model was shown a stub it never saw - the one thing a note about
    # what the model did not see cannot afford to be wrong about.
    surviving_elided = [entry for entry in elided if alive[entry["index"]]]
    trimmed = bool(surviving_elided) or calls_dropped > 0 or messages_dropped > 0
    marker = (
        _marker_text(len(dropped_turns), messages_dropped, len(surviving_elided))
        if trimmed
        else ""
    )

    sent = [{"role": "system", "content": marker}] if marker else []
    sent.extend(slots[index] for index in range(len(slots)) if alive[index])

    kept = sum(message_bytes(slots[index]) for index in range(protected) if alive[index])
    charged = kept + (message_bytes(sent[0]) if marker else 0)
    return sent, {
        "budget_bytes": int(budget_bytes),
        "trimmed": trimmed,
        "has_marker": bool(marker),
        "head_marker": marker,
        "turns_dropped": len(dropped_turns),
        "dropped": dropped_turns,
        "messages_dropped": int(messages_dropped),
        "tool_results_elided": len(surviving_elided),
        "elided": surviving_elided,
        "calls_dropped": int(calls_dropped),
        "amnesia": bool(amnesia),
        "repaired": len(repaired),
        "protected_from": int(protected),
        "sent_settled": int(settled_sent),
        "before_bytes": int(settled_before),
        "after_bytes": int(charged),
        "fits": bool(
            kept + (MARKER_RESERVE_BYTES if trimmed else 0) <= budget_bytes
        ),
        "order": EVICTION_ORDER,
    }


# ---------------------------------------------------------------------------
# What the panel says about it (ticket 14 §5)
# ---------------------------------------------------------------------------

def trim_meta(previous: dict | None, report: dict, now: str | None = None) -> dict:
    """Ticket 14 §5's `meta.context_trim`, with one turn folded in.

    Informational, and deliberately so: the header chip and the percentage of the
    store the model can see are always recomputed from the projection, never read
    from here. This is what a bug report and the file's own history are for, and a
    stored flag that disagreed with reality would be worse than no flag at all.
    """
    meta = dict(previous or {})
    meta.setdefault("first_trim_at", now or store.now_iso())
    meta["turns_elided_total"] = int(meta.get("turns_elided_total") or 0) + int(
        report["turns_dropped"]
    )
    meta["last_trim"] = {
        "turns": int(report["turns_dropped"]),
        "messages": int(report["messages_dropped"]),
        "tool_results_elided": int(report["tool_results_elided"]),
    }
    return meta


def note_line(report: dict) -> str:
    """The note's one sentence: ticket 14 §5's, with the counts that are real.

    One note per turn and never silence: an agent that quietly forgets is a trust
    bug, and the same sentence is what the stored `meta.context_trim` is for.
    """
    parts = []
    if report["tool_results_elided"]:
        count = report["tool_results_elided"]
        parts.append(f"{count} tool result" + ("" if count == 1 else "s") + " elided")
    if report["calls_dropped"]:
        count = report["calls_dropped"]
        parts.append(f"{count} tool call" + ("" if count == 1 else "s") + " dropped")
    if report["turns_dropped"]:
        count = report["turns_dropped"]
        parts.append(f"{count} turn" + ("" if count == 1 else "s") + " dropped")
    if not parts:
        count = report["messages_dropped"]
        parts.append(f"{count} message" + ("" if count == 1 else "s") + " dropped")
    return f"{NOTE_PREFIX} \u2014 model saw " + ", ".join(parts) + "; your history is kept"


def note_detail(report: dict) -> str:
    """The expander: which turns went, how much room there was, and why."""
    lines = [
        f'{report["before_bytes"]} B of settled history, '
        f'{report["budget_bytes"]} B budget',
        report["order"],
    ]
    for turn in report["dropped"]:
        opened = f'\u201c{turn["prompt"]}\u201d' if turn.get("prompt") else "(no user message)"
        lines.append(f"turn {turn['turn']}: {opened} \u2014 {turn['messages']} messages")
    for entry in report["elided"]:
        lines.append(
            f'{entry["tool"] or "tool"} output elided: '
            f'{entry["bytes_before"]} B \u2192 {entry["bytes_after"]} B'
        )
    if report["calls_dropped"]:
        lines.append(f'{report["calls_dropped"]} call/result pairs dropped with their calls')
    if report["amnesia"]:
        lines.append("the turn in flight is over budget, so every settled turn went")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The invariants, checked without asking `plan` what it did
# ---------------------------------------------------------------------------

def _same_slot(stored_message: dict, sent_message: dict) -> bool:
    """Whether a sent message is a stored one, possibly edited in place.

    Exactly two edits are legal before the turn in flight: a tool result's content
    replaced by the elision stub (its `tool_call_id` is what identifies it), and an
    assistant's `tool_calls` reduced with its prose untouched. Anything else has to
    be identical, which is what makes "a subsequence with in-place edits" (I5) a
    check rather than an assertion about `plan`'s internals.
    """
    if stored_message.get("role") != sent_message.get("role"):
        return False
    if sent_message.get("role") == "tool":
        return stored_message.get("tool_call_id") == sent_message.get("tool_call_id")
    if sent_message.get("role") == "assistant":
        if stored_message.get("content") != sent_message.get("content"):
            return False
        stored_ids = [call.get("id") for call in stored_message.get("tool_calls") or []]
        return all(
            call.get("id") in stored_ids
            for call in sent_message.get("tool_calls") or []
        )
    return stored_message == sent_message


def validate_projection(stored: list[dict], sent: list[dict], protected_from=None) -> list[str]:
    """Every way `sent` fails to be a sendable projection of `stored`.

    Written from ticket 14 §4's invariants and from the two lists alone - it does
    not consult `plan`'s report, and it re-derives the structure it needs - so a
    bug in the eviction order shows up here as a violation instead of being
    mirrored by the code under test. Returns human-readable problems; `[]` means
    the projection is sound.

    `protected_from` is the index in `stored` where the turn in flight begins. It
    turns on two things the caller knows and this function cannot: I4 (that slice
    must be byte-identical) and I2's documented exception (a call that turn has not
    run yet may be unanswered).
    """
    problems: list[str] = []
    body = [message for message in sent if message.get("role") != "system"]

    tail_count = 0
    if protected_from is not None and 0 <= int(protected_from) <= len(stored):
        tail_count = len(stored) - int(protected_from)
    tail = body[len(body) - tail_count :] if tail_count else []
    if tail_count and tail != list(stored[int(protected_from) :]):
        problems.append("I4: the turn in flight is not byte-identical to the store")

    # I5/I7: every sent message is a stored message, and they are still in order.
    position = 0
    for message in body:
        while position < len(stored) and not _same_slot(stored[position], message):
            position += 1
        if position >= len(stored):
            problems.append(
                f"I5: a {message.get('role')!r} message is not in the store, or is out of order"
            )
            break
        position += 1

    seen_calls: set = set()
    answered: set = set()
    for message in body:
        if message.get("role") == "assistant":
            if not str(message.get("content") or "").strip() and not message.get("tool_calls"):
                problems.append("I3: an assistant message with neither prose nor calls")
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id in seen_calls:
                    problems.append(f"I7: call id {call_id!r} appears twice")
                seen_calls.add(call_id)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in seen_calls:
                problems.append(f"I1: tool result {call_id!r} has no earlier call")
            if call_id in answered:
                problems.append(f"I7: call id {call_id!r} is answered twice")
            answered.add(call_id)

    # I2, minus the calls the turn in flight has not run yet.
    exempt = {
        call.get("id")
        for message in tail
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    leftovers = [call_id for call_id in seen_calls if call_id not in answered and call_id not in exempt]
    if leftovers:
        problems.append(f"I2: calls with no result in the projection: {leftovers}")
    return problems
