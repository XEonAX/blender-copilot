"""Conversation state: the transcript, and the turn that grows it.

Nothing here imports `bpy`, so the whole model is testable on plain CPython
(`tests/test_conversation.py`) - which is the only way to test it at all,
because `bpy.app.timers` never pump under `blender -b`.

The producer is elsewhere and is deliberately invisible here: `transport.py`
launches a worker subprocess, `stream.py` drains it on a timer, and both hand
plain dicts to `Conversation.apply_event`. That method is the single place an
event becomes visible state, which is why it lives in the bpy-free half.

The message kinds are the ones the layout prototype established - user turn,
assistant prose, model code, tool call, error - because the layouts in
`panel.py` still render all of them. Only the first two are produced so far;
the tools pass fills in the rest.
"""

from __future__ import annotations

import json
import re

# Rough character budget for one panel label. 5.2.2 gives a panel no wrapping
# control, no monospace font and no rich text, so long lines are wrapped by
# hand. The value is a guess tuned to the default 280 px sidebar; the box
# variant loses a few characters per line to the border and inset.
WRAP_CHARS = 38
BOX_WRAP_CHARS = 34

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

# What the panel is doing *now*, for the Stop control. `None` means idle.
BUSY_STREAM = "stream"

# Thinking is billed as completion tokens and can arrive for many seconds
# before the first visible character (measured, ticket 16), so the panel shows it
# as a status change rather than as reply text. Only every REASONING_NOTCH
# characters, so a long think does not repaint the sidebar once per token.
REASONING_NOTCH = 40

# Ticket 09 §6: one redaction boundary, applied where text becomes visible, so
# no error path and no crash report can leak a key into the transcript.
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


def redact(text: str) -> str:
    if not text:
        return text
    return _KEY_PATTERN.sub("sk-\u2026", text)


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
        self.last_receipt: dict | None = None
        # Reasoning characters seen this turn. Never shown as reply text; it
        # only moves the status between "waiting" and "thinking".
        self.reasoning_chars = 0
        self._reasoning_notch = 0
        # Set only when the transport cannot run at all - no key, no worker.
        # A *turn* failure (401, 429, a dropped stream) is a transcript block
        # instead, because sending again is a real option there.
        self.transport_error: str | None = None

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
        # Deliberately terse: the header row shares the sidebar's width with the
        # "prototype" chip, and Blender middle-clips a label that does not fit.
        if self.streaming:
            if not self._reply_text():
                return "thinking\u2026" if self.reasoning_chars else "waiting\u2026"
            return "streaming\u2026"
        if self.running_tool is not None:
            return "running code"
        return "idle"

    @property
    def running_tool(self) -> Message | None:
        for message in reversed(self.messages):
            if message.kind == KIND_TOOL and message.status == STATUS_RUNNING:
                return message
        return None

    # -- the turn ------------------------------------------------------------
    def _reply_text(self) -> str:
        if not self.messages:
            return ""
        last = self.messages[-1]
        return last.text if last.kind == KIND_ASSISTANT else ""

    def _drop_empty_reply(self) -> None:
        """Ticket 09 §5: a turn that produced nothing commits nothing."""
        if (
            self.messages
            and self.messages[-1].kind == KIND_ASSISTANT
            and not self.messages[-1].text
        ):
            self.messages.pop()

    def begin_turn(self, prompt: str) -> None:
        """Open a turn: the user's message, then the empty assistant block the
        streamed reply grows into. Called after the request is on the wire and
        before the drain timer starts, so no event can arrive without a turn."""
        prompt = prompt.strip()
        if not prompt or self.streaming:
            return
        self.messages.append(Message("user", prompt))
        self.messages.append(Message("assistant", ""))
        self.streaming = True
        self.busy_kind = BUSY_STREAM
        self.reasoning_chars = 0
        self._reasoning_notch = 0

    def append_text(self, text: str) -> bool:
        if not self.streaming or not text:
            return False
        self.messages[-1].text += text
        return True

    def finish_turn(self, reason: str | None = None) -> bool:
        """Close a turn. `reason` is the provider's `finish_reason`, when known.

        `length` earns its own block: the reply is *incomplete*, which is a
        different fact from "the model stopped", and thinking counts against the
        same budget (ticket 16) so it can happen with no visible text at all.
        """
        if not self.streaming:
            return False
        self.streaming = False
        self.busy_kind = None
        if reason == "length":
            self._drop_empty_reply()
            self.messages.append(
                Message(
                    "assistant",
                    kind=KIND_ERROR,
                    text="The reply hit the token limit and is incomplete. Thinking "
                    "counts against that budget, so this can happen mid-thought.",
                    detail='{"finish_reason": "length"}',
                )
            )
        elif not self._reply_text():
            self._drop_empty_reply()
            self.messages.append(
                Message(
                    "assistant",
                    kind=KIND_ERROR,
                    text="The model returned nothing. Nothing ran and nothing changed.",
                )
            )
        return True

    def fail_turn(self, kind: str, message: str, detail: str = "") -> bool:
        """End the turn with a first-class error block (ticket 08 §4).

        Nothing is retried - not a request, not a tool call (ticket 09 §6) - so
        this is a dead end the user leaves by sending again.
        """
        if not self.streaming:
            return False
        self.streaming = False
        self.busy_kind = None
        self._drop_empty_reply()
        self.messages.append(
            Message("assistant", kind=KIND_ERROR, text=redact(message), detail=redact(detail))
        )
        return True

    def set_transport_error(self, message: str | None) -> bool:
        message = redact(message) if message else None
        if message == self.transport_error:
            return False
        self.transport_error = message
        return True

    def wire_messages(self, user_text: str, base_prompt: str, summary: str) -> list[dict]:
        """The request body: base prompt, the conversation, the prompt, the summary.

        Only the transcript's **prose** becomes wire messages. Error blocks, code
        blocks and tool rows are display, not conversation - sending an error
        block back as an assistant turn would ask the model to continue from a
        message it never wrote.

        The live summary is the **trailing** message rather than part of index 0
        (ticket 14's correction to ticket 09 §4, confirmed accepted by the
        provider in ticket 16), so the base prompt stays byte-identical across
        turns and the provider's cache prefix survives.

        Ticket 14's degradation projection is deliberately **not** here: nothing
        is trimmed or summarized, because with no tools a turn is a few hundred
        bytes and the projection belongs with the store it is a projection *of*.
        """
        messages: list[dict] = [{"role": "system", "content": base_prompt}]
        for message in self.messages:
            if message.kind == KIND_USER and message.text:
                messages.append({"role": "user", "content": message.text})
            elif message.kind == KIND_ASSISTANT and message.text:
                messages.append({"role": "assistant", "content": message.text})
        messages.append({"role": "user", "content": user_text.strip()})
        messages.append({"role": "system", "content": summary})
        return messages

    def apply_event(self, event: dict) -> bool:
        """One worker event in, visible state out. True when a repaint is owed.

        Every branch returns whether anything the user can see has changed,
        because the drain timer repaints on exactly that signal and never on a
        tick count.
        """
        kind = event.get("ev")
        if kind == "delta":
            return self.append_text(event.get("text") or "")
        if kind == "reasoning":
            self.reasoning_chars += int(event.get("chars") or 0)
            notch = self.reasoning_chars // REASONING_NOTCH
            if notch != self._reasoning_notch:
                self._reasoning_notch = notch
                return True
            return False
        if kind == "done":
            return self.finish_turn(event.get("finish_reason"))
        if kind == "stopped":
            return self.cancel()
        if kind in ("error", "startup_failed", "exit"):
            fatal = bool(event.get("fatal"))
            if fatal:
                # A turn cannot even start without a worker, so this is the
                # panel's persistent state rather than a transcript block
                # (ticket 11: "fail visibly", Send disabled, Retry offered).
                self.set_transport_error(event.get("message") or "The worker is unavailable.")
            detail = event.get("detail") or json.dumps(
                {k: v for k, v in event.items() if k not in ("ev", "message")}, indent=2
            )
            changed = self.fail_turn(
                event.get("kind") or kind,
                event.get("message") or "Request failed.",
                detail,
            )
            return changed or fatal
        return False

    def cancel(self) -> bool:
        """Stop the in-flight stream, keep what arrived, mark it stopped."""
        if not self.streaming:
            return False
        self.streaming = False
        self.busy_kind = None
        if self.messages and self.messages[-1].kind == KIND_ASSISTANT:
            marker = self.messages[-1]
            if marker.text:
                marker.text = marker.text.rstrip() + " [stopped]"
            else:
                self._drop_empty_reply()
        return True

    def toggle(self, index: int) -> None:
        if 0 <= index < len(self.messages):
            message = self.messages[index]
            message.expanded = not message.expanded

    def clear(self) -> None:
        """Discard the conversation. Deliberately does **not** clear
        `transport_error`: whether the worker can run is not a property of the
        transcript."""
        self.messages.clear()
        self.streaming = False
        self.busy_kind = None
        self.last_receipt = None
        self.reasoning_chars = 0
        self._reasoning_notch = 0


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


# The transcript starts **empty**, deliberately. `_demo()` is now a fixture for
# the layout checks (`tests/test_conversation.py`, `tools/panel_draw_smoke.py`) -
# one conversation covering every kind the panel can render - and is no longer
# seeded into the live session. A fabricated transcript sitting in a panel that
# runs real turns is indistinguishable from a real one, and that is the one
# thing a chat surface must never be.
