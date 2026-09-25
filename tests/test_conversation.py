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

# ---------------------------------------------------------------- clearing
roles.clear()
check("clear empties the conversation", roles.messages == [] and roles.status == "idle")

print("\nall checks passed")
