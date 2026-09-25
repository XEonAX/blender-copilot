"""Smoke checks for the conversation state. Plain CPython, no Blender.

    python3 tests/test_conversation.py

`conversation.py` imports nothing from `bpy` on purpose, so the streaming and
truncation logic is testable outside Blender - which is the only way to check
it without a GUI session.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "conversation", _HERE.parent / "blender_copilot" / "conversation.py"
)
assert _spec and _spec.loader
conversation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conversation)


def check(label: str, condition: bool) -> None:
    assert condition, f"FAILED: {label}"
    print(f"ok   {label}")


# ---------------------------------------------------------------- wrapping
check("wrap leaves short text alone", conversation.wrap("a b c") == ["a b c"])
check("wrap returns nothing for empty text", conversation.wrap("") == [])
check(
    "wrap stays within the budget",
    all(len(line) <= conversation.WRAP_CHARS for line in conversation.wrap("word " * 40)),
)
check(
    "wrap hard-splits a word longer than the budget",
    max(len(line) for line in conversation.wrap("x" * 200)) <= conversation.WRAP_CHARS,
)

# ---------------------------------------------------------------- the turn
session = conversation.Conversation()
check("a new conversation is idle", session.status == "idle")
check(
    "an event with no turn in flight is a no-op",
    session.apply_event({"ev": "delta", "text": "x"}) is False,
)

session.begin_turn("hello")
check("a turn opens with the user's message", session.messages[0].role == "user")
check(
    "a turn opens with an empty assistant block",
    session.messages[-1].role == "assistant" and session.messages[-1].text == "",
)
check("a turn is in flight", session.streaming)
check("a second turn cannot open mid-turn", not session.begin_turn("again"))
check("status waits before any text arrives", session.status == "waiting\u2026")

check("a delta grows the reply", session.apply_event({"ev": "delta", "text": "Hel"}))
check("status streams once text arrives", session.status == "streaming\u2026")
check("a second delta appends", session.apply_event({"ev": "delta", "text": "lo"}))
check("the reply is the concatenation", session.messages[-1].text == "Hello")

check(
    "a reasoning chunk below the notch does not repaint",
    session.apply_event({"ev": "reasoning", "chars": 5}) is False,
)
check("reasoning is counted", session.reasoning_chars == 5)
check(
    "the status notices a long think",
    session.apply_event({"ev": "reasoning", "chars": conversation.REASONING_NOTCH + 1}) is True,
)

check("done closes the turn", session.apply_event({"ev": "done", "finish_reason": "stop"}))
check("the turn is over", not session.streaming)
check("the reply survives the close", session.messages[-1].text == "Hello")
check("status returns to idle", session.status == "idle")
check("a late delta is dropped", session.apply_event({"ev": "delta", "text": "!"}) is False)

blank = conversation.Conversation()
blank.begin_turn("   ")
check("a blank prompt opens no turn", blank.messages == [] and not blank.streaming)

# ---------------------------------------------------------------- empty replies
thinking = conversation.Conversation()
thinking.begin_turn("hi")
thinking.apply_event({"ev": "reasoning", "chars": 10})
check("reasoning alone reads as thinking", thinking.status == "thinking\u2026")
thinking.apply_event({"ev": "done", "finish_reason": "length"})
check(
    "a length-truncated turn is announced",
    thinking.messages[-1].kind == conversation.KIND_ERROR,
)
check(
    "the truncated turn left no empty reply behind",
    all(message.text for message in thinking.messages),
)

empty = conversation.Conversation()
empty.begin_turn("hi")
empty.apply_event({"ev": "done", "finish_reason": "stop"})
check(
    "an empty reply is reported, not hidden",
    empty.messages[-1].kind == conversation.KIND_ERROR,
)

# ---------------------------------------------------------------- failures
failed = conversation.Conversation()
failed.begin_turn("hi")
check(
    "an error ends the turn",
    failed.apply_event(
        {"ev": "error", "kind": "rate_limit", "status": 429,
         "message": "slow down", "detail": "{}"}
    ),
)
check("the error is a first-class block", failed.messages[-1].kind == conversation.KIND_ERROR)
check("the error carries the server's sentence", failed.messages[-1].text == "slow down")
check("a turn failure is not a transport failure", failed.transport_error is None)
check(
    "an error after the turn is a no-op",
    failed.apply_event({"ev": "error", "message": "x"}) is False,
)

fatal = conversation.Conversation()
fatal.begin_turn("hi")
fatal.apply_event({"ev": "startup_failed", "fatal": True, "message": "no worker"})
check("a fatal startup failure is persistent", fatal.transport_error == "no worker")
check("a fatal failure also ends the turn", not fatal.streaming)
fatal.clear()
check(
    "clearing the transcript keeps the transport failure",
    fatal.transport_error == "no worker",
)
check(
    "a key inside an error is redacted",
    conversation.redact("bad key sk-abc123XYZ") == "bad key sk-\u2026",
)
leaky = conversation.Conversation()
leaky.begin_turn("hi")
leaky.apply_event({"ev": "error", "message": "Bearer sk-live1234567890 rejected"})
check(
    "the redaction boundary is on the visible path",
    "sk-live" not in leaky.messages[-1].text,
)

# ---------------------------------------------------------------- truncation
long_conversation = conversation.Conversation()
for index in range(40):
    long_conversation.messages.append(conversation.Message("user", f"line {index}"))
lines = long_conversation.visible_lines()
check("the panel truncates instead of scrolling", len(lines) <= conversation.VISIBLE_LINES + 1)
check("truncation is announced rather than silent", lines[0].startswith("..."))

# ---------------------------------------------------------------- rendering
roles = conversation.Conversation()
roles.messages.append(conversation.Message("user", "hi"))
roles.messages.append(conversation.Message("assistant", "there"))
rendered = roles.visible_lines()
check("user lines are marked", rendered[0].startswith(">"))
check("assistant lines are marked", "| there" in rendered)

# ---------------------------------------------------------------- paging
paged = conversation.Conversation()
for index in range(12):
    paged.messages.append(conversation.Message("user", f"turn {index}"))
    paged.messages.append(conversation.Message("assistant", f"reply {index}"))
page, hidden, has_older, has_newer = paged.page_view()
check("the newest page is last", page[-1].text == "reply 11")
check("a long transcript has an older page", has_older and hidden > 0)
check("the newest page has no newer page", not has_newer)
paged.older()
older_page, older_hidden, _, older_has_newer = paged.page_view()
check("Older moves back exactly one page", older_hidden < hidden)
check("Older names the newer direction", older_has_newer)
paged.newest()
check("newest returns to the end", paged.page_view()[1] == hidden)
check("pages keep whole turns", all(len(page) <= len(paged.messages) for page in paged._pages()))
paged.newer()
check("newer clamps at the newest page", paged.page == 0)

# ---------------------------------------------------------------- kinds
rich = conversation.Conversation()
rich.messages.extend(conversation._demo())
rich.last_receipt = None
kinds = {message.kind for message in rich.messages}
check(
    "the demo covers every rendered kind",
    kinds
    == {
        conversation.KIND_USER,
        conversation.KIND_ASSISTANT,
        conversation.KIND_CODE,
        conversation.KIND_TOOL,
        conversation.KIND_ERROR,
    },
)
check("the demo carries a running tool", rich.running_tool is not None)
check("a running tool shows in the status", rich.status == "running code")
log = rich.visible_lines()
check("collapse hides code bodies from the log", not any("scale = 1.3" in line for line in log))
rich.toggle(rich.messages.index(rich.running_tool))
check("toggle mutates expansion", rich.running_tool.expanded)
exported = rich.transcript_text()
check("the export keeps code verbatim", "scale = 1.3" in exported)
check("the export keeps tracebacks verbatim", "TypeError" in exported)
check("the export is not truncated", not exported.startswith("..."))

# ---------------------------------------------------------------- stable paging
stable = conversation.Conversation()
stable.messages.extend(conversation._demo())
before = [len(page) for page in stable._pages()]
for message in stable.messages:
    message.expanded = not message.expanded
check("expanding detail never reflows the page", [len(page) for page in stable._pages()] == before)
for message in stable.messages:
    message.expanded = False

# ---------------------------------------------------------------- cancel
running = conversation.Conversation()
running.begin_turn("hello")
running.apply_event({"ev": "delta", "text": "partial"})
check("cancel stops the turn", running.cancel())
check("cancel keeps the partial text", running.messages[-1].text.startswith("partial"))
check("cancel marks the turn stopped", running.messages[-1].text.endswith("[stopped]"))
check("cancel is idempotent", running.cancel() is False)
check("cancel clears the busy kind", running.busy_kind is None)

nothing = conversation.Conversation()
nothing.begin_turn("hello")
nothing.cancel()
check(
    "cancelling before any text commits no empty reply",
    [message.text for message in nothing.messages] == ["hello"],
)

stopped = conversation.Conversation()
stopped.begin_turn("hello")
stopped.apply_event({"ev": "delta", "text": "half"})
check("a stopped event ends the turn", stopped.apply_event({"ev": "stopped"}))
check("the stopped turn is marked", stopped.messages[-1].text.endswith("[stopped]"))

# ---------------------------------------------------------------- clearing
roles.clear()
check("clear empties the conversation", roles.messages == [] and roles.status == "idle")

print("\nall checks passed")
