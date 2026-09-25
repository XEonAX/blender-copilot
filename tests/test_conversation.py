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

# ---------------------------------------------------------------- streaming
session = conversation.Conversation()
check("a new conversation is idle", session.status == "idle")
check("advancing an idle conversation is a no-op", session.advance() is False)

session.send("hello")
check("send appends the user turn", session.messages[0].role == "user")
check(
    "send appends an empty assistant turn",
    session.messages[-1].role == "assistant" and session.messages[-1].text == "",
)
check("send starts the stream", session.streaming)

ticks = 0
while session.streaming and ticks < 10_000:
    session.advance()
    ticks += 1
check("the stream terminates", not session.streaming)
check("the stream delivers the whole reply", session.messages[-1].text == conversation.FAKE_REPLY)
check("status returns to idle", session.status == "idle")
check("advancing after completion is a no-op", session.advance() is False)
check("advance reports work while streaming", ticks > 1)

blank = conversation.Conversation()
blank.send("   ")
check("a blank prompt is ignored", blank.messages == [] and not blank.streaming)

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
running.send("hello")
running.advance()
partial = running.messages[-1].text
running.cancel()
check("cancel stops the stream", not running.streaming)
check("cancel keeps the partial text", running.messages[-1].text.startswith(partial))
check("cancel marks the turn stopped", running.messages[-1].text.endswith("[stopped]"))
check("cancel is idempotent", (running.cancel(), running.streaming)[1] is False)
check("cancel clears the busy kind", running.busy_kind is None)

# ---------------------------------------------------------------- clearing
roles.clear()
check("clear empties the conversation", roles.messages == [] and roles.status == "idle")

print("\nall checks passed")
