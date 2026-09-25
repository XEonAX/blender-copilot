"""Conversation state for the panel prototype.

No model is called, nothing is persisted, and nothing here imports `bpy` - so
the whole model is testable on plain CPython (`tests/test_conversation.py`).

This revision exists for one ticket: *How a conversation is laid out and
controlled*. The state now carries the message kinds that layout has to
distinguish - user turn, assistant prose, model code, tool call (running /
done / failed) and error - so the three candidate layouts in `panel.py` all
render the *same* conversation and can be compared honestly.

Still deliberately module-level Python with a `send` / `advance` shape, because
that is the shape the real implementation needs: a producer appends off the
main thread, a `bpy.app.timers` callback drains it on the main thread.
"""

from __future__ import annotations

# Rough character budget for one panel label. 5.2.2 gives a panel no wrapping
# control, no monospace font and no rich text, so long lines are wrapped by
# hand. The value is a guess tuned to the default 280 px sidebar; the box
# variant loses a few characters per line to the border and inset.
WRAP_CHARS = 38
BOX_WRAP_CHARS = 34

# A Panel cannot scroll, so a transcript page is bounded and the rest is
# reached with explicit Older / Newer buttons. Budget is in rendered lines,
# whole messages only, so a page never cuts a turn in half.
PAGE_LINES = 34

# The log variant's flat line budget (kept for the original prototype tests).
VISIBLE_LINES = 26

# Message kinds. `user` and `assistant` are prose; the rest belong to the
# assistant's turn and have an expandable `detail` body.
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_CODE = "code"
KIND_TOOL = "tool"
KIND_ERROR = "error"

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"

# Addon-owned Text datablocks. Code is mirrored here because the panel cannot
# show it unwrapped or selectable; see the ticket answer.
CODE_TEXT = "Copilot Code"
TRANSCRIPT_TEXT = "Copilot Transcript"

# The honest coverage sentence, shown once and persistently under the input
# (settled by *What replaces undo as the recovery mechanism?*).
COVERAGE_LINES = (
    "Ctrl+Z undoes one agent turn.",
    "Files, network and preferences cannot be undone.",
)

FAKE_REPLY = (
    "I resolved the target to the active object. The change is one call; I "
    "will show the code and then run it."
)


class Message:
    """One transcript entry. `text` is prose and is what the stream grows."""

    __slots__ = ("role", "text", "kind", "detail", "status", "purpose", "expanded")

    def __init__(
        self,
        role: str,
        text: str = "",
        kind: str | None = None,
        detail: str = "",
        status: str | None = None,
        purpose: str = "",
        expanded: bool = False,
    ) -> None:
        self.role = role
        self.text = text
        self.kind = kind or ("user" if role == "user" else "assistant")
        self.detail = detail
        self.status = status
        self.purpose = purpose
        self.expanded = expanded


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


def gutter(text: str, prefix: str = "  ") -> list[str]:
    """Detail bodies (code, tool output, traceback) get a plain-text inset."""
    return [f"{prefix}{line}" for line in text.splitlines()]


class Conversation:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.streaming = False
        # What is running *now*, for the Stop control: None | "stream" | "tool"
        self.busy_kind: str | None = None
        self.page = 0  # 0 = newest page; larger = further back
        self.last_receipt: dict | None = None
        self._source = ""
        self._cursor = 0

    # -- rendering: shared block model ---------------------------------------
    def _line_count(self, message: Message) -> int:
        return len(message.detail.splitlines()) or 1

    def _log_block(self, message: Message) -> list[str]:
        """The flat, prefix-only rendering. Also the paging cost estimate."""
        if message.kind == KIND_CODE:
            lines = [f"| code · {self._line_count(message)} lines"]
            if message.expanded:
                lines += gutter(message.detail)
            return lines
        if message.kind == KIND_TOOL:
            mark = {
                STATUS_RUNNING: "...",
                STATUS_OK: "+",
                STATUS_ERROR: "x",
            }.get(message.status, "?")
            lines = [f"| {mark} {message.purpose or message.text}"]
            if message.expanded and message.detail:
                lines += gutter(message.detail)
            return lines
        if message.kind == KIND_ERROR:
            lines = [f"| ! {message.text}"]
            if message.expanded and message.detail:
                lines += gutter(message.detail)
            return lines
        prefix = ">" if message.role == "user" else "|"
        width = WRAP_CHARS if prefix == ">" else WRAP_CHARS - 2
        return [f"{prefix} {chunk}" for chunk in wrap(message.text, width)]

    def visible_lines(self) -> list[str]:
        """Log-variant transcript: oldest first, truncated at the old end."""
        lines: list[str] = []
        for message in self.messages:
            lines.extend(self._log_block(message))
            lines.append("")
        if len(lines) > VISIBLE_LINES:
            dropped = len(lines) - VISIBLE_LINES
            lines = [f"... {dropped} earlier lines dropped"] + lines[-VISIBLE_LINES:]
        return lines

    # -- rendering: paging ---------------------------------------------------
    def _cost(self, message: Message) -> int:
        """Paging cost is computed as if every detail row were collapsed, so
        expanding a code block never reflows the page under the user."""
        if message.kind in (KIND_CODE, KIND_TOOL, KIND_ERROR):
            return 2
        width = WRAP_CHARS if message.role == "user" else WRAP_CHARS - 2
        return max(1, len(wrap(message.text, width))) + 1

    def _pages(self, budget: int = PAGE_LINES) -> list[list[Message]]:
        """Whole-message pages packed from the *newest* end, so the page the
        user actually sees first is full and the oldest page holds leftovers."""
        pages: list[list[Message]] = []
        current: list[Message] = []
        used = 0
        for message in reversed(self.messages):
            cost = self._cost(message)
            if current and used + cost > budget:
                pages.append(list(reversed(current)))
                current = []
                used = 0
            current.append(message)
            used += cost
        if current:
            pages.append(list(reversed(current)))
        pages.reverse()
        return pages or [[]]

    def page_view(self, budget: int = PAGE_LINES) -> tuple[list[Message], int, bool, bool]:
        """A whole-message page plus (hidden_before, has_older, has_newer)."""
        pages = self._pages(budget)
        index = max(0, min(len(pages) - 1, len(pages) - 1 - self.page))
        self.page = len(pages) - 1 - index
        hidden = sum(len(page) for page in pages[:index])
        return pages[index], hidden, index > 0, index < len(pages) - 1

    def older(self) -> None:
        self.page += 1

    def newer(self) -> None:
        self.page = max(0, self.page - 1)

    def newest(self) -> None:
        self.page = 0

    # -- rendering: full export ---------------------------------------------
    def transcript_text(self) -> str:
        """Everything, untruncated, for the companion Text datablock."""
        chunks: list[str] = []
        for message in self.messages:
            if message.kind == KIND_CODE:
                chunks.append(f"[code · {message.purpose or 'untitled'}]")
                chunks.append(message.detail)
            elif message.kind == KIND_TOOL:
                chunks.append(f"[tool {message.status or '?'} · {message.purpose}]")
                if message.detail:
                    chunks.append(message.detail)
            elif message.kind == KIND_ERROR:
                chunks.append(f"[error] {message.text}")
                if message.detail:
                    chunks.append(message.detail)
            else:
                speaker = "you" if message.role == "user" else "copilot"
                chunks.append(f"{speaker}: {message.text}")
            chunks.append("")
        return "\n".join(chunks)

    # -- status --------------------------------------------------------------
    @property
    def status(self) -> str:
        if self.streaming:
            return f"streaming {self._cursor}/{len(self._source)}"
        if self.running_tool is not None:
            return "running code"
        return "idle"

    @property
    def running_tool(self) -> Message | None:
        for message in reversed(self.messages):
            if message.kind == KIND_TOOL and message.status == STATUS_RUNNING:
                return message
        return None

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
        self.busy_kind = "stream"
        self.newest()

    def advance(self, chunk: int = 4) -> bool:
        """Append up to `chunk` characters. True while a repaint is owed."""
        if not self.streaming:
            return False
        self._cursor = min(len(self._source), self._cursor + chunk)
        self.messages[-1].text = self._source[: self._cursor]
        if self._cursor >= len(self._source):
            self.streaming = False
            self.busy_kind = None
        return True

    def cancel(self) -> None:
        """Stop the in-flight stream, keep what arrived, mark it stopped."""
        if not self.streaming:
            return
        self.streaming = False
        self.busy_kind = None
        marker = self.messages[-1]
        if marker.kind == KIND_ASSISTANT:
            marker.text = (marker.text + " [stopped]").strip()

    def toggle(self, index: int) -> None:
        if 0 <= index < len(self.messages):
            message = self.messages[index]
            message.expanded = not message.expanded

    def clear(self) -> None:
        self.messages.clear()
        self.streaming = False
        self.busy_kind = None
        self.page = 0
        self.last_receipt = None
        self._source = ""
        self._cursor = 0


session = Conversation()


# --------------------------------------------------------------------------
# The demo conversation. Static, and deliberately covering every state the
# layout has to render: prose, code, a finished tool, a failed tool with an
# expandable traceback, and a tool still running.
# --------------------------------------------------------------------------

DEMO_CODE = """obj = bpy.context.active_object
if obj is None:
    raise RuntimeError("nothing is active")

scale = 1.3
obj.scale = (obj.scale.x, obj.scale.y, obj.scale.z * scale)
bpy.context.view_layer.update()
print(f"{obj.name}: z scale -> {obj.scale.z:.2f}")"""

DEMO_CODE_2 = """mesh = bpy.context.active_object.data
if not mesh.materials:
    mesh.materials.append(None)
mesh.materials[0] = bpy.data.materials["Red"]"""

DEMO_TRACEBACK = """Traceback (most recent call last):
  File "<model>", line 3, in <module>
TypeError: 'NoneType' object is not subscriptable
  File "bpy_types.py", line 427, in __getattr__
    raise AttributeError(name)"""


def _demo() -> list[Message]:
    return [
        Message("user", "What is selected right now?"),
        Message(
            "assistant",
            kind=KIND_ERROR,
            text="Request failed: HTTP 429 (rate limited). The turn is incomplete; "
            "no code ran and nothing changed.",
            detail='{"error": {"kind": "api_error", "status": 429}}',
        ),
        Message("user", "What is selected right now?"),
        Message(
            "assistant",
            "Nothing is inspecting the scene yet - this prototype has no tools. "
            "The real loop would answer from get_scene_info.",
        ),
        Message(
            "assistant",
            kind=KIND_TOOL,
            status=STATUS_OK,
            purpose="Summarise scene",
            detail='{"ok": true, "object_count": 3, "active": {"name": "Cube"}}',
        ),
        Message("user", "Make the cube taller."),
        Message(
            "assistant",
            "I resolved \u201cthis\u201d to the active object, Cube, and scaled it 1.3x on Z:",
        ),
        Message(
            "assistant",
            kind=KIND_CODE,
            purpose="Scale Cube 1.3x on Z",
            detail=DEMO_CODE,
        ),
        Message(
            "assistant",
            kind=KIND_TOOL,
            status=STATUS_OK,
            purpose="Scale Cube 1.3x on Z",
            detail="Cube.dimensions.z: 2.00 -> 2.60",
        ),
        Message("user", "Now give it a red material, and bevel the edges."),
        Message(
            "assistant",
            "Two calls, material first. The mesh has no material slots yet, so "
            "the link call fails:",
        ),
        Message(
            "assistant",
            kind=KIND_CODE,
            purpose="Create and link red material",
            detail=DEMO_CODE_2,
        ),
        Message(
            "assistant",
            kind=KIND_TOOL,
            status=STATUS_ERROR,
            purpose="Link red material",
            detail=DEMO_TRACEBACK,
        ),
        Message(
            "assistant",
            "The link failed - the mesh had no material slots. I append an "
            "empty slot first and retry:",
        ),
        Message(
            "assistant",
            kind=KIND_CODE,
            purpose="Add slot, then link",
            detail=DEMO_CODE_2,
        ),
        Message(
            "assistant",
            kind=KIND_TOOL,
            status=STATUS_RUNNING,
            purpose="Link red material (retry)",
        ),
    ]


def seed_once() -> None:
    """Fill the transcript the first time the add-on registers."""
    if session.messages:
        return
    session.messages.extend(_demo())
    session.last_receipt = {
        "undoable": True,
        "summary": "1 object changed - Cube scaled on Z",
        "coverage": "Ctrl+Z reverts that turn; Ctrl+Alt+Z opens Blender's undo history",
    }
