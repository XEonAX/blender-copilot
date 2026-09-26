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
`panel.py` render all of them, and all of them are produced now.

Two transcripts live here, and conflating them is the bug this file is shaped to
prevent:

  * `self.messages` is the **display** transcript: prose, code identity rows, tool
    rows, error blocks. It is what the panel draws.
  * `self.history` is the **wire** transcript: verbatim provider messages,
    including the assistant message that carries `tool_calls` and the `tool`
    results that answer it. A history missing either half of that pair is rejected
    outright by the provider (HTTP 400, measured in ticket 16), so it is kept as
    the provider's own format rather than reconstructed from the display.

The turn itself is a per-tick state machine (ticket 09 §0): `REQUESTING` while a
reply streams, `TOOL_QUEUE` while queued calls run one per timer tick, then
either back to `REQUESTING` for the next round or out of the turn. `pump()` is
that machine's clock and `stream.py` is its only caller. The three things it needs
from outside - how to send a request, how to run a call, where the live scene
summary comes from - are injected through `attach()`, because two of them need
`bpy` and everything worth testing here does not.
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
BUSY_TOOL = "tool"

# The loop's phase (ticket 09 §0). `streaming` answers "is a turn in flight";
# this answers "where in the turn are we", which is what decides whether the next
# tick runs a tool call or waits for a reply.
PHASE_IDLE = "idle"
PHASE_REQUEST = "request"
PHASE_TOOL = "tool"

# The caps, transcribed from ticket 09 §1. Why 8 rounds: a normal ask is 2-4
# rounds (look, act, verify), so 8 allows one self-repair without eating the
# context window. 24 calls bounds a single round that returns many parallel calls.
# The repeat detector fires on the *third* identical call - tool plus canonical
# arguments - and the failure stop counts rounds, not calls.
MAX_ROUNDS = 8
MAX_TOOL_CALLS = 24
REPEAT_LIMIT = 3
FAIL_STREAK_LIMIT = 3

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
    """One transcript entry. `text` is prose and is what the stream grows.

    `call_id` is the wire `tool_call` this row reports, when it is a tool row. It
    is what lets a result find the row that was drawn for it, which is the whole
    mechanism behind "one permanent row per call, never removed" (ticket 08 §3).
    """

    __slots__ = (
        "role", "text", "kind", "detail", "status", "purpose", "expanded", "call_id",
    )

    def __init__(
        self,
        role: str,
        text: str = "",
        kind: str | None = None,
        detail: str = "",
        status: str | None = None,
        purpose: str = "",
        expanded: bool = False,
        call_id: str = "",
    ) -> None:
        self.role = role
        self.text = text
        self.kind = kind or ("user" if role == "user" else "assistant")
        self.detail = detail
        self.status = status
        self.purpose = purpose
        self.expanded = expanded
        self.call_id = call_id


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


def _plain_arguments(call: dict) -> dict | None:
    """The call's arguments as a dict, or None when they cannot be read.

    Measured (ticket 16, and AGENTS.md): `tool_calls[].function.arguments`
    arrives as a **JSON string, not an object**, and the worker concatenated it
    from the SSE fragments. The loop needs the parsed form twice - to label the
    row and to canonicalise a signature - so it parses here; `execution.py` parses
    again to *validate*, which is a different question with a different answer.
    """
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _signature(call: dict) -> str:
    """Tool plus canonical arguments: one call's identity, for the repeat detector.

    Canonical, not literal: the same code with its keys in another order is the
    same call, and a detector that could not see that would let a model spin by
    reordering a dict.
    """
    function = call.get("function") or {}
    arguments = _plain_arguments(call)
    if arguments is None:
        canonical = str(function.get("arguments"))
    else:
        canonical = json.dumps(arguments, sort_keys=True, default=str)
    return f"{function.get('name') or ''} {canonical}"


def _synthetic_result(tool: str, kind: str, message: str) -> dict:
    """The loop's own result for a call that never ran.

    Built here rather than in `execution.py` because it is not an execution: it is
    the loop answering for a call the user stopped or a cap refused, and it has to
    be constructible without Blender in the room.
    """
    envelope = {
        "ok": False,
        "tool": tool,
        "summary": message,
        "error": {"kind": kind, "message": message},
    }
    content = json.dumps(envelope, ensure_ascii=False)
    return {
        "ok": False,
        "envelope": envelope,
        "content": content,
        "detail": json.dumps(envelope, ensure_ascii=False, indent=2),
        "summary": message,
    }


def _envelope(content) -> dict | None:
    """A tool result's envelope, when it parses. On the wire it is JSON text."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _row_detail(content) -> str:
    """The expandable body of a restored tool row: pretty JSON, or the raw text."""
    envelope = _envelope(content)
    if envelope is None:
        return redact(content) if isinstance(content, str) else ""
    return json.dumps(envelope, ensure_ascii=False, indent=2)


def messages_from_history(history: list[dict]) -> list[Message]:
    """Rebuild the display transcript from the stored wire history.

    The store keeps **one** schema - the provider's - so a reopened file re-derives
    its rows instead of carrying a second, lossier one (*Where chat history lives*
    §3). What that costs, said plainly rather than hidden: a block that only ever
    explained something *live* - the transport banner, a cap's notice, the
    `[stopped]` marker - has no wire form and does not come back. What the model
    saw and what it answered does.

    The call/result pairing is re-made here, by id, and nowhere else: a `tool`
    result is drawn as the row of the call that produced it, so a restored
    transcript shows the same shape a live one did - code row, then tool row.
    """
    results: dict = {}
    for message in history:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id:
                results[call_id] = message

    rows: list[Message] = []
    for message in history:
        role = message.get("role")
        if role == "user":
            text = redact(message.get("content")) if isinstance(message.get("content"), str) else ""
            if text:
                rows.append(Message("user", text))
            continue
        if role != "assistant":
            # A `tool` result is drawn with its call, and a `system` message is
            # the base prompt or the live summary - neither is part of the
            # conversation the user had.
            continue
        text = redact(message.get("content")) if isinstance(message.get("content"), str) else ""
        if text:
            rows.append(Message("assistant", text))
        for call in message.get("tool_calls") or []:
            arguments = _plain_arguments(call)
            function = call.get("function") or {}
            purpose = ""
            code = None
            if isinstance(arguments, dict):
                purpose = str(arguments.get("purpose") or "").strip()
                code = arguments.get("code")
            label = purpose or function.get("name") or "tool call"
            call_id = call.get("id") or ""
            if isinstance(code, str) and code.strip():
                rows.append(
                    Message(
                        "assistant",
                        kind=KIND_CODE,
                        purpose=label,
                        detail=code,
                        call_id=call_id,
                    )
                )
            result = results.get(call_id)
            if result is None:
                # An unanswered call is an error, never `running`: a restored row
                # stuck at running would make the panel claim a turn is in flight
                # for the rest of the session.
                rows.append(
                    Message(
                        "assistant",
                        kind=KIND_TOOL,
                        status=STATUS_ERROR,
                        purpose=label,
                        call_id=call_id,
                        detail='{"ok": false, "note": "No result was recorded for this call."}',
                    )
                )
                continue
            envelope = _envelope(result.get("content"))
            rows.append(
                Message(
                    "assistant",
                    kind=KIND_TOOL,
                    status=STATUS_OK if envelope and envelope.get("ok") else STATUS_ERROR,
                    purpose=label,
                    detail=_row_detail(result.get("content")),
                    call_id=call_id,
                )
            )
    return rows


class Conversation:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.streaming = False
        # What is running *now*, for the Stop control: None | "stream" | "tool"
        self.busy_kind: str | None = None
        self.last_receipt: dict | None = None
        # The wire transcript. Never the display transcript: see the module
        # docstring for why they are two lists and not one.
        self.history: list[dict] = []
        # The scope generation. Bumped whenever this conversation stops being the
        # one an in-flight turn belonged to - a file was opened, or the session
        # was pointed at a different conversation - and compared against
        # `turn_generation` on every event, so the old worker's late chunks are
        # dropped instead of landing in a file they know nothing about.
        self.generation = 0
        self.turn_generation = 0
        # The loop (ticket 09 §0). `phase` is where in the turn we are; the rest is
        # the ledger the caps are computed from and the queue the ticks drain.
        self.phase = PHASE_IDLE
        self.pending: list[dict] = []
        self.send = None
        self.execute = None
        self.context = None
        self._rows: dict = {}
        self._rounds = 0
        self._calls = 0
        self._signatures: list[str] = []
        self._round_open = False
        self._round_calls = 0
        self._round_failures = 0
        self._fail_streak = 0
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
        #
        # The running call is checked FIRST: between two queued calls the turn is
        # still in flight with no stream to grow, and "running code" is what the
        # Stop note underneath it is about.
        if self.running_tool is not None:
            return "running code"
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
        before the drain timer starts, so no event can arrive without a turn.

        The round counter starts at 1 because the first request of the turn is
        already in flight by the time this runs; `pump()` counts every round after
        it.
        """
        prompt = prompt.strip()
        if not prompt or self.streaming:
            return
        self.messages.append(Message("user", prompt))
        self.messages.append(Message("assistant", ""))
        self.history.append({"role": "user", "content": prompt})
        self.streaming = True
        self.turn_generation = self.generation
        self.phase = PHASE_REQUEST
        self.busy_kind = BUSY_STREAM
        self.reasoning_chars = 0
        self._reasoning_notch = 0
        self._rows = {}
        self.pending = []
        self._rounds = 1
        self._calls = 0
        self._signatures = []
        self._round_open = False
        self._round_calls = 0
        self._round_failures = 0
        self._fail_streak = 0

    def append_text(self, text: str) -> bool:
        # Only while a reply is actually streaming: a late delta arriving during
        # TOOL_QUEUE would otherwise grow whatever row happens to be last.
        if not self.streaming or self.phase != PHASE_REQUEST or not text:
            return False
        self.messages[-1].text += text
        return True

    def finish_reply(self, reason: str | None = None, tool_calls: list | None = None) -> bool:
        """A reply finished: either it asked for tools, or the turn is over.

        `reason` is the provider's `finish_reason`; `tool_calls` is the accumulated
        array the worker assembled from the fragmented deltas.

        `length` is terminal whatever else arrived (ticket 09 §6): a truncated
        `tool_calls` is *incomplete*, not wrong, so its calls are dropped rather
        than half-run - and dropping them before they reach history is what keeps
        the turn sendable again, because no id is then left unanswered.
        """
        if not self.streaming or self.phase != PHASE_REQUEST:
            return False
        if reason == "length":
            self._end_turn()
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
            return True

        text = self._reply_text()
        if tool_calls:
            # The wire message goes in *before* anything runs: from here on, every
            # id in it needs a matching `tool` result, and the provider enforces
            # that (ticket 16, HTTP 400).
            self.history.append(
                {"role": "assistant", "content": text or None, "tool_calls": tool_calls}
            )
            if not text:
                self._drop_empty_reply()
            self._queue_calls(tool_calls)
            self.phase = PHASE_TOOL
            self.busy_kind = BUSY_TOOL
            return True

        if text:
            self.history.append({"role": "assistant", "content": text})
        else:
            self._drop_empty_reply()
            self.messages.append(
                Message(
                    "assistant",
                    kind=KIND_ERROR,
                    text="The model returned nothing. Nothing ran and nothing changed.",
                )
            )
        self._end_turn()
        return True

    def fail_turn(self, kind: str, message: str, detail: str = "") -> bool:
        """End the turn with a first-class error block (ticket 08 §4).

        Nothing is retried - not a request, not a tool call (ticket 09 §6) - so
        this is a dead end the user leaves by sending again.
        """
        if not self.streaming:
            return False
        # A turn can end with calls still queued (the transport died mid-round).
        # They still need answers, or the next send is refused.
        self._flush_pending("not_run", "The turn ended before this call ran.")
        self._drop_empty_reply()
        self.messages.append(
            Message("assistant", kind=KIND_ERROR, text=redact(message), detail=redact(detail))
        )
        self._end_turn()
        return True

    def set_transport_error(self, message: str | None) -> bool:
        message = redact(message) if message else None
        if message == self.transport_error:
            return False
        self.transport_error = message
        return True

    def wire_messages(self, user_text: str, base_prompt: str, summary: str) -> list[dict]:
        """The first request of a turn: base prompt, history, the prompt, the summary.

        The live summary is the **trailing** message rather than part of index 0
        (ticket 14's correction to ticket 09 §4, confirmed accepted by the provider
        in ticket 16), so the base prompt stays byte-identical across turns and the
        provider's cache prefix survives.

        The new prompt is passed in rather than read from the transcript, and is
        not appended to history here: `begin_turn` does that, and the caller builds
        the request *before* opening the turn (otherwise the user's message would
        be in both places). Rounds after the first are built by `pump` through
        `build_messages`, where the history already ends with the tool results.

        Ticket 14's degradation projection is deliberately **not** here: nothing is
        trimmed or summarized, because the projection belongs with the store it is
        a projection *of*.
        """
        return self.build_messages(base_prompt, summary, user_text)

    def build_messages(
        self, base_prompt: str, summary: str, user_text: str | None = None
    ) -> list[dict]:
        """The provider's message list: `[system, *history, (user), system]`.

        The history dicts are copied shallowly, so a caller cannot reach back into
        the stored conversation by holding on to the list it was handed.
        """
        messages: list[dict] = [{"role": "system", "content": base_prompt}]
        messages.extend(dict(message) for message in self.history)
        if user_text and user_text.strip():
            messages.append({"role": "user", "content": user_text.strip()})
        messages.append({"role": "system", "content": summary})
        return messages

    def apply_event(self, event: dict) -> bool:
        """One worker event in, visible state out. True when a repaint is owed.

        Every branch returns whether anything the user can see has changed,
        because the drain timer repaints on exactly that signal and never on a
        tick count.

        The generation fence comes first. An event that arrives after the file
        underneath the turn changed belongs to a scene this conversation is not
        looking at - and the fatal ones are the reason the fence exists: without
        it, the previous file's dying worker would put "Send is unavailable" on a
        freshly opened file that has nothing wrong with it.
        """
        if self.generation != self.turn_generation:
            return False
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
            return self.finish_reply(
                event.get("finish_reason"), event.get("tool_calls") or []
            )
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
        """Stop the turn, keep what arrived, and leave the history sendable again.

        Three cases, one rule (ticket 09 §5). A stream caught mid-reply keeps its
        partial text and commits it. A stream caught before any text commits
        nothing, so a cancelled turn cannot add an empty assistant message. And
        calls that were queued but never ran get a synthetic `cancelled` result
        each, because the assistant message that asked for them is already in
        history and the provider rejects a call it cannot match (ticket 16).
        """
        if not self.streaming:
            return False
        self._flush_pending("cancelled", "Cancelled by user")
        if (
            self.phase == PHASE_REQUEST
            and self.messages
            and self.messages[-1].kind == KIND_ASSISTANT
        ):
            marker = self.messages[-1]
            if marker.text:
                partial = marker.text
                marker.text = partial.rstrip() + " [stopped]"
                self.history.append({"role": "assistant", "content": partial})
            else:
                self._drop_empty_reply()
        self._end_turn()
        return True

    def toggle(self, index: int) -> None:
        if 0 <= index < len(self.messages):
            message = self.messages[index]
            message.expanded = not message.expanded

    # -- the loop (ticket 09 §0) ---------------------------------------------
    def attach(self, send, execute, context) -> None:
        """Wire the loop's three collaborators.

        Nothing here may import `bpy` - the CPython checks are the only way to
        exercise a turn at all, because `bpy.app.timers` never pump under
        `blender -b` - so the bpy half is injected instead. `stream.py` supplies
        the real ones; the checks supply fakes, through this same seam. The
        contracts are deliberately tiny:

          * `send(messages) -> str | None` - one request. A returned string is a
            reason it could not be sent, and ends the turn.
          * `execute(call) -> dict` - one tool call, returning `{ok, content,
            detail, summary}`. `content` is the wire `tool` message, and the loop
            never looks inside it.
          * `context() -> (base_prompt, summary)` - the stable prompt and the live
            scene summary, both read fresh for every request (ticket 09 §4).
        """
        self.send = send
        self.execute = execute
        self.context = context

    def pump(self) -> bool:
        """Exactly one bounded step of the turn's state machine, or nothing.

        `stream.py` calls this once per timer tick and nothing else drives it.
        That is what makes "at most one tool call per tick" true, and why it
        matters is the `running…` row: exec is synchronous on the main thread, so
        a tick that ran two calls could never paint the first one's row before the
        second overwrote it.

        The ticks a turn spends in `TOOL_QUEUE` are therefore one per call, plus
        one at the end to either stop for a cap or ask for the next round.
        """
        if self.phase != PHASE_TOOL or self.execute is None:
            return False
        if not self.pending:
            self._close_round()
        reason = self._cap_reason()
        if reason:
            return self._stop_with(reason)
        if self.pending:
            self._run_call(self.pending.pop(0))
            return True
        return self._send_round()

    def _send_round(self) -> bool:
        """Ask for the next round. The reply arrives later, as events."""
        if self.send is None or self.context is None:
            return False
        base_prompt, summary = self.context()
        problem = self.send(self.build_messages(base_prompt, summary))
        if problem:
            # A round that cannot go out is a turn that cannot continue, and the
            # same two-part answer as a worker that never started: the persistent
            # transport block, and an error block closing the turn.
            self.set_transport_error(problem)
            self.fail_turn("worker_unavailable", problem)
            return True
        self._rounds += 1
        self.phase = PHASE_REQUEST
        self.busy_kind = BUSY_STREAM
        self.reasoning_chars = 0
        self._reasoning_notch = 0
        self.messages.append(Message("assistant", ""))
        return True

    def _close_round(self) -> None:
        """A round's calls have all been consumed: fold them into the ledger.

        The consecutive-failure stop is a property of a *round*, so it can only be
        decided here, once, at the boundary - not while calls are still running.
        """
        if not self._round_open:
            return
        self._round_open = False
        if self._round_calls and self._round_failures == self._round_calls:
            self._fail_streak += 1
        else:
            self._fail_streak = 0

    def _cap_reason(self) -> str | None:
        """Why the turn stops now, or None (ticket 09 §1).

        Checked at the two boundaries a tick can stop at: before the next queued
        call, and before the next request. Both are the `TOOL_QUEUE → REQUESTING`
        boundary in the sense that matters - the turn cannot advance without
        passing through them - and the call cap has to be checked *before* a call
        runs or it would not bound a round that returns thirty of them.
        """
        if self._calls >= MAX_TOOL_CALLS:
            return (
                f"Stopped at the {MAX_TOOL_CALLS}-call limit for one turn. "
                'Send "continue" to keep going.'
            )
        if self.pending:
            if self._signatures.count(_signature(self.pending[0])) >= REPEAT_LIMIT - 1:
                return (
                    "Stopped: the same call was asked for a third time. "
                    'Send "continue" to keep going.'
                )
            return None
        if self._rounds >= MAX_ROUNDS:
            return f'Stopped after {MAX_ROUNDS} rounds. Send "continue" to keep going.'
        if self._fail_streak >= FAIL_STREAK_LIMIT:
            return (
                f"Stopped after {FAIL_STREAK_LIMIT} rounds in a row where every call "
                'failed. Send "continue" to keep going.'
            )
        return None

    def _stop_with(self, reason: str) -> bool:
        """Stop for a cap and *report* it - never unwind a thing (ticket 09 §3).

        What the turn did is on screen, the row's output is behind its expander,
        and one Ctrl+Z is the only revert. The loop does not attempt a cleanup
        call and does not synthesise a "partial success": `exec` is not
        transactional and only the model can decide what to do about a half-applied
        change.
        """
        self._flush_pending("not_run", reason)
        self._end_turn()
        self.messages.append(Message("assistant", kind=KIND_ERROR, text=reason))
        return True

    def _run_call(self, call: dict) -> None:
        """Execute one queued call and put its result on both transcripts."""
        self._calls += 1
        self._signatures.append(_signature(call))
        self._round_calls += 1
        result = self.execute(call) or {}
        if not result.get("ok"):
            self._round_failures += 1
        call_id = call.get("id") or ""
        self.history.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result.get("content") or "",
            }
        )
        row = self._rows.get(call_id)
        if row is not None:
            row.status = STATUS_OK if result.get("ok") else STATUS_ERROR
            row.detail = result.get("detail") or ""

    def _flush_pending(self, kind: str, message: str) -> None:
        """Answer every queued call that will never run.

        Load-bearing rather than tidy (ticket 09 §5): the assistant message that
        asked for these calls is already in history, and the provider refuses a
        `tool_calls` it cannot match with a `tool` result - HTTP 400, "must be
        followed by tool messages responding to each `tool_call_id`" (measured in
        ticket 16). One flusher serves the Stop path, the cap path and the
        transport-failure path, so none of them can leave an orphan behind.
        """
        while self.pending:
            call = self.pending.pop(0)
            call_id = call.get("id") or ""
            tool = (call.get("function") or {}).get("name") or ""
            result = _synthetic_result(tool, kind, message)
            self.history.append(
                {"role": "tool", "tool_call_id": call_id, "content": result["content"]}
            )
            row = self._rows.get(call_id)
            if row is not None:
                row.status = STATUS_ERROR
                row.detail = result["detail"]

    def _queue_calls(self, tool_calls: list) -> None:
        """Put a round's calls on the queue and in the transcript.

        Both rows of a call's audit trail are made here, while the call is still
        `running…`: the code identity row (ticket 08 §2 - the panel shows what the
        code *is*, and the full text lives in the `Copilot Code` datablock behind
        `Show code`) and the permanent tool row the result will fill in.
        """
        self._round_open = True
        self._round_calls = 0
        self._round_failures = 0
        for call in tool_calls:
            function = call.get("function") or {}
            arguments = _plain_arguments(call)
            purpose = ""
            code = None
            if isinstance(arguments, dict):
                purpose = str(arguments.get("purpose") or "").strip()
                code = arguments.get("code")
            label = purpose or function.get("name") or "tool call"
            call_id = call.get("id") or ""
            if isinstance(code, str) and code.strip():
                code_row = Message(
                    "assistant", kind=KIND_CODE, purpose=label, detail=code, call_id=call_id
                )
                self.messages.append(code_row)
            row = Message(
                "assistant",
                kind=KIND_TOOL,
                status=STATUS_RUNNING,
                purpose=label,
                call_id=call_id,
            )
            self.messages.append(row)
            self._rows[call_id] = row
            self.pending.append(call)

    def _end_turn(self) -> None:
        """Terminal state. Every path out of a turn comes through here, so no
        path can leave `phase`, `streaming` and the queue disagreeing."""
        self.streaming = False
        self.phase = PHASE_IDLE
        self.busy_kind = None
        self.pending = []
        self._round_open = False

    def clear(self) -> None:
        """Discard the conversation. Deliberately does **not** clear
        `transport_error`: whether the worker can run is not a property of the
        transcript."""
        self.messages.clear()
        self.history.clear()
        self._end_turn()
        self.last_receipt = None
        self.reasoning_chars = 0
        self._reasoning_notch = 0

    # -- leaving and re-entering a conversation ------------------------------
    def abandon(self) -> bool:
        """Let go of the in-flight turn because the file it belonged to has gone.
        True when there was one to let go of.

        Nothing is written into the arriving conversation - no "you switched
        files" line, because the store *is* the provider's wire format and does not
        take UI-only lines. What the *old* conversation keeps is a truthful ending:
        its queued calls are answered with `scope_changed`, so the history left
        behind is still sendable when the user comes back to that file.

        The generation bump is the other half, and `turn_generation` deliberately
        stays where the abandoned turn started it - the next `begin_turn` re-syncs
        it - so every late event is dropped rather than applied.
        """
        had_turn = self.streaming
        if had_turn:
            self._flush_pending("scope_changed", "The file changed before this call ran.")
            self._drop_empty_reply()
            self._end_turn()
        self.generation += 1
        return had_turn

    def snapshot(self) -> list[dict]:
        """The wire history, copied, for the store to write."""
        return [dict(message) for message in self.history]

    def restore(self, history: list[dict]) -> None:
        """Adopt a stored conversation: the wire history and the rows it implies.

        Both transcripts are replaced together, because a display transcript that
        disagrees with the wire one is the bug the two-list split exists to
        prevent. `abandon`'s discipline applies too - the generation moves - so an
        event from whatever was running before this conversation was opened cannot
        land in it.
        """
        self._end_turn()
        self.history[:] = [dict(message) for message in history]
        self.messages[:] = messages_from_history(self.history)
        self.last_receipt = None
        self.reasoning_chars = 0
        self._reasoning_notch = 0
        self._rows = {}
        self.generation += 1


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
            "The real loop answers this from get_scene_info. This transcript is "
            "a static fixture, so the reply is canned.",
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
