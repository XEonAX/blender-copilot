"""Conversation state for the panel prototype.

No model is called, nothing is persisted. This exists to prove two things a
panel needs before anything else can be built on it:

  1. that a panel can repaint text that grows over time, and
  2. that a multi-line prompt can be captured.

The state here is deliberately plain module-level Python with a `send` /
`advance` shape, because that is exactly the shape the real implementation
needs: a producer that appends text somewhere off the main thread, and a
consumer that the `bpy.app.timers` callback drains on the main thread. See the
map for the route from here to a real agent loop.
"""

from __future__ import annotations

# Rough character budget for one panel label. 5.2.2 gives a panel no wrapping
# control, no monospace font and no rich text, so long lines are wrapped by hand
# and the width is a guess tuned to the default sidebar width.
WRAP_CHARS = 38

# A Panel cannot scroll, so the transcript is truncated rather than hidden.
# Older lines are dropped with a marker; this is the limit biting.
VISIBLE_LINES = 26

FAKE_REPLY = (
    "Here is the smallest change that does it. I will select the active object, "
    "then move it up by one unit:\n"
    "\n"
    "    obj = bpy.context.active_object\n"
    "    obj.location.z += 1.0\n"
    "\n"
    "Note the code above renders as plain text: a 5.2 panel has no rich text, "
    "so there is no code styling anywhere in this transcript."
)

_SEED = (
    ("user", "What is selected right now?"),
    ("assistant", "Prototype only - nothing is inspecting the scene yet."),
)


class Message:
    __slots__ = ("role", "text")

    def __init__(self, role: str, text: str) -> None:
        self.role = role
        self.text = text


def wrap(text: str, width: int = WRAP_CHARS) -> list[str]:
    """Wrap on whitespace, hard-splitting any word longer than the budget."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > width:
            lines.append(word[:width])
            word = word[width:]
        current = word
    if current:
        lines.append(current)
    return lines


class Conversation:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.streaming = False
        self._source = ""
        self._cursor = 0

    # -- rendering -----------------------------------------------------------
    def visible_lines(self) -> list[str]:
        lines: list[str] = []
        for message in self.messages:
            prefix = ">" if message.role == "user" else "|"
            for chunk in wrap(message.text):
                lines.append(f"{prefix} {chunk}")
            lines.append("")
        if len(lines) > VISIBLE_LINES:
            dropped = len(lines) - VISIBLE_LINES
            lines = [f"... {dropped} earlier lines dropped"] + lines[-VISIBLE_LINES:]
        return lines

    @property
    def status(self) -> str:
        if self.streaming:
            return f"streaming {self._cursor}/{len(self._source)}"
        return "idle"

    # -- the fake producer ---------------------------------------------------
    def send(self, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt:
            return
        self.messages.append(Message("user", prompt))
        self.messages.append(Message("assistant", ""))
        self._source = FAKE_REPLY
        self._cursor = 0
        self.streaming = True

    def advance(self, chunk: int = 4) -> bool:
        """Append up to `chunk` characters. Returns True if nothing else matters.

        Mirrors the real drain loop: it is called from a timer, and the caller
        only needs to know whether the panel has to repaint.
        """
        if not self.streaming:
            return False
        self._cursor = min(len(self._source), self._cursor + chunk)
        self.messages[-1].text = self._source[: self._cursor]
        if self._cursor >= len(self._source):
            self.streaming = False
        return True

    def clear(self) -> None:
        self.messages.clear()
        self.streaming = False
        self._source = ""
        self._cursor = 0


session = Conversation()


def seed_once() -> None:
    """Fill the transcript the first time the add-on registers."""
    if session.messages:
        return
    for role, text in _SEED:
        session.messages.append(Message(role, text))
