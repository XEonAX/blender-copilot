"""Smoke checks for the conversation state. Plain CPython, no Blender.

    python3 tests/test_conversation.py

`conversation.py` and `execution.py` import nothing from `bpy` on purpose, so the
streaming, the loop and the sandbox are all testable outside Blender - which is
the only way to check them, because `bpy.app.timers` never pump without a GUI.

The loop is driven through its own seam: `session.attach` takes the three things
that need `bpy` (how to send a request, how to run a call, where the live summary
comes from), and these checks pass fakes. Nothing here reaches a network or a
scene.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent

# `budget.py` FIRST, and registered under the name `execution._sibling` and
# `conversation._sibling` resolve to. `run_python` catches `budget.Exceeded` by
# class identity, so a second copy of the module - which is what loading by path
# gives you for free - would be a second, unrelated exception class and the catch
# would silently miss. One module object, deliberately, before anything that
# imports it is loaded.
_budget_spec = importlib.util.spec_from_file_location(
    "bc_budget", _HERE.parent / "blender_copilot" / "budget.py"
)
assert _budget_spec and _budget_spec.loader
budget = importlib.util.module_from_spec(_budget_spec)
sys.modules["bc_budget"] = budget
_budget_spec.loader.exec_module(budget)

_spec = importlib.util.spec_from_file_location(
    "conversation", _HERE.parent / "blender_copilot" / "conversation.py"
)
assert _spec and _spec.loader
conversation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conversation)

_exec_spec = importlib.util.spec_from_file_location(
    "execution", _HERE.parent / "blender_copilot" / "execution.py"
)
assert _exec_spec and _exec_spec.loader
execution = importlib.util.module_from_spec(_exec_spec)
_exec_spec.loader.exec_module(execution)

_undo_spec = importlib.util.spec_from_file_location(
    "undo", _HERE.parent / "blender_copilot" / "undo.py"
)
assert _undo_spec and _undo_spec.loader
undo = importlib.util.module_from_spec(_undo_spec)
_undo_spec.loader.exec_module(undo)

_context_spec = importlib.util.spec_from_file_location(
    "context", _HERE.parent / "blender_copilot" / "context.py"
)
assert _context_spec and _context_spec.loader
context = importlib.util.module_from_spec(_context_spec)
_context_spec.loader.exec_module(context)


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

# ---------------------------------------------------------------- no pager
# The pager lost at the 2026-09-26 visual pass: the whole transcript renders and
# the sidebar REGION scrolls. These guard the public surface, because the failure
# they prevent is the *unused* pager - the thing that let two contradictory
# designs sit in one file and both read as live.
check("no page-size constant", not hasattr(conversation, "PAGE_LINES"))
check("a session has no page cursor", not hasattr(conversation.Conversation(), "page"))
check(
    "no paging on the conversation",
    not any(
        hasattr(conversation.Conversation, name)
        for name in ("page_view", "older", "newer", "newest")
    ),
)

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
        conversation.KIND_NOTE,
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


# ==========================================================================
# The sandbox. Real `exec`, no Blender: the prelude is whatever dict is passed.
# ==========================================================================
print("\n-- run_blender_python --")

OK = {}
ok_run = execution.run_python("print('hello')", "Say hello", {})
check("a call that returns is ok", ok_run["ok"] is True)
check("stdout is captured", ok_run["envelope"]["stdout"].strip() == "hello")
check("the envelope is the wire content", json.loads(ok_run["content"])["ok"] is True)
check("the envelope carries the purpose", ok_run["envelope"]["purpose"] == "Say hello")
check("the detail is the pretty envelope", "\n" in ok_run["detail"])

fresh = execution.run_python("x = 1", "Define x", {})
check("the second call is ok", fresh["ok"] is True)
later = execution.run_python("print(x)", "Read x", {})
check(
    "a name from one call is not visible in the next",
    later["ok"] is False and later["envelope"]["error"]["type"] == "NameError",
)
check("and the failure names the line it happened on", later["envelope"]["error"]["line"] == 1)

boom = execution.run_python("print('got here')\nraise RuntimeError('boom')", "Fail", {})
check("a raise is a failure, not a crash", boom["ok"] is False)
check(
    "the FULL traceback comes back",
    "Traceback (most recent call last)" in boom["envelope"]["error"]["traceback"]
    and "RuntimeError: boom" in boom["envelope"]["error"]["traceback"],
)
check("the failing line is the model's line", boom["envelope"]["error"]["line"] == 2)
check("stdout from before the raise is kept", boom["envelope"]["stdout"].strip() == "got here")
check("the failure reaches the model as ok:false", json.loads(boom["content"])["ok"] is False)

bad_syntax = execution.run_python("if True\n  pass", "Bad syntax", {})
check(
    "a syntax error is reported with its own line",
    bad_syntax["envelope"]["error"]["type"] == "SyntaxError"
    and bad_syntax["envelope"]["error"]["line"] == 1,
)

exiting = execution.run_python("import sys; sys.exit(3)", "Exit", {})
check(
    "SystemExit cannot take the loop with it",
    exiting["ok"] is False and exiting["envelope"]["error"]["type"] == "SystemExit",
)

noisy = execution.run_python("print('x' * 20000)", "Print a lot", {})
check(
    "stdout is capped, and says it was",
    noisy["envelope"]["truncated"] is True
    and "chars elided" in noisy["envelope"]["stdout"]
    and len(noisy["envelope"]["stdout"]) <= execution.STDOUT_CAP,
)

stderr_run = execution.run_python(
    "import sys; sys.stderr.write('y' * 9000)", "Write a lot to stderr", {}
)
check(
    "stderr is capped, and says it was",
    stderr_run["envelope"]["truncated"] is True
    and len(stderr_run["envelope"]["stderr"]) <= execution.STDERR_CAP,
)


# ==========================================================================
# Dispatch: the model's arguments are a JSON *string*, and everything it can get
# wrong has an envelope rather than an exception.
# ==========================================================================
print("\n-- the tool's envelope --")


def wire_call(call_id, purpose, code, name=execution.RUN_PYTHON):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"code": code, "purpose": purpose}),
        },
    }


bound = execution.execute_tool(
    wire_call("a1", "Read the tag", "print(bpy.tag)"),
    lambda: {"bpy": SimpleNamespace(tag="live")},
)
check("arguments arrive as a string and are parsed", bound["ok"] is True)
check("the prelude reached the code", bound["envelope"]["stdout"].strip() == "live")

# The dict form was in the docstring and the callers but not the code: it fell
# through the `callable` test and came back empty, so seven wire-probe checks
# failed while every check here passed. Pin it.
literal = execution.execute_tool(
    wire_call("a2", "Read the tag", "print(bpy.tag)"),
    {"bpy": SimpleNamespace(tag="dict-live")},
)
check(
    "a bare dict prelude is used as-is",
    literal["envelope"]["stdout"].strip() == "dict-live",
)

unknown = execution.execute_tool(
    {"id": "a2", "function": {"name": "frobnicate", "arguments": "{}"}}, {}
)
check(
    "an unknown tool is named and runs nothing",
    unknown["ok"] is False
    and unknown["envelope"]["error"]["kind"] == "tool_argument_error"
    and "frobnicate" in unknown["envelope"]["error"]["message"],
)

broken = execution.execute_tool(
    {"id": "a3", "function": {"name": execution.RUN_PYTHON, "arguments": "{oops"}}, {}
)
check(
    "malformed arguments come back as raw text the model can fix",
    broken["envelope"]["error"]["kind"] == "tool_argument_error"
    and broken["envelope"]["error"]["raw"] == "{oops",
)

no_purpose = execution.execute_tool(
    {"id": "a4", "function": {"name": execution.RUN_PYTHON, "arguments": '{"code": "pass"}'}},
    {},
)
check(
    "a missing purpose is a self-explaining argument error",
    no_purpose["ok"] is False and "purpose" in no_purpose["envelope"]["error"]["message"],
)

long_purpose = execution.execute_tool(
    wire_call("a5", "p" * 200, "pass"), {}
)
check("an over-long purpose is refused", long_purpose["ok"] is False)
check("no code ran in that case", "stdout" not in long_purpose["envelope"] or not long_purpose["envelope"]["stdout"])

not_a_dict = execution.parse_arguments(
    {"function": {"name": execution.RUN_PYTHON, "arguments": "[1, 2]"}}
)
check("arguments that are valid JSON but not an object are rejected", not_a_dict is None)
check(
    "a malformed arguments string is handled at the boundary, not raised",
    execution.parse_arguments({"function": {"arguments": "{oops"}}) is None,
)


# ==========================================================================
# The loop. This is the seam: a fake sender, a fake executor, no Blender.
# ==========================================================================
print("\n-- the loop --")


class FakeSender:
    """A transport that never touches a socket: it records what a round carried."""

    def __init__(self):
        self.sent: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> str | None:
        self.sent.append(messages)
        return None


def ok_executor(call):
    return {
        "ok": True,
        "envelope": {"ok": True},
        "content": '{"ok": true}',
        "detail": '{\n  "ok": true\n}',
        "summary": "ok",
    }


def fail_executor(call):
    return {
        "ok": False,
        "envelope": {"ok": False},
        "content": '{"ok": false, "error": {"kind": "exec_error"}}',
        "detail": '{\n  "ok": false\n}',
        "summary": "failed",
    }


def wire(session, sender, execute=ok_executor, undo=None):
    session.attach(
        send=sender, execute=execute, context=lambda: ("BASE", "LIVE SUMMARY"), undo=undo
    )
    return session


def deliver(session, text="", calls=None, reason="stop"):
    """The transport delivering a finished reply, exactly as stream.py does it."""
    if text:
        session.apply_event({"ev": "delta", "text": text})
    return session.apply_event(
        {"ev": "done", "finish_reason": reason, "tool_calls": list(calls or [])}
    )


def unanswered(history):
    """Ids a `tool_calls` message asked about that no `tool` message answered.

    This is the provider's own rule, measured in ticket 16: HTTP 400,
    "must be followed by tool messages responding to each tool_call_id".
    """
    asked = [
        call_id
        for message in history
        if message.get("role") == "assistant"
        for call_id in [entry.get("id") for entry in (message.get("tool_calls") or [])]
    ]
    answered = [
        message.get("tool_call_id") for message in history if message.get("role") == "tool"
    ]
    return [call_id for call_id in asked if call_id not in answered]


bullet = conversation.Conversation()
sender = FakeSender()
wire(bullet, sender)
bullet.begin_turn("make the cube taller")
check("a turn opens in the request phase", bullet.phase == conversation.PHASE_REQUEST)
check("and it is in flight", bullet.streaming)
check(
    "the user's message is the first wire message",
    bullet.history == [{"role": "user", "content": "make the cube taller"}],
)

deliver(bullet, "The cube is active, so I'll scale it.", [wire_call("c1", "Scale Cube 1.5x on Z", "pass")])
check("a reply with tool calls moves the turn to the queue", bullet.phase == conversation.PHASE_TOOL)
check(
    "the assistant message with tool_calls is on the wire history",
    bullet.history[-1]["role"] == "assistant" and bullet.history[-1]["tool_calls"][0]["id"] == "c1",
)
check(
    "the prose that came with the calls is kept in history",
    bullet.history[-1]["content"] == "The cube is active, so I'll scale it.",
)
check(
    "the panel shows a code row naming what will run",
    any(
        message.kind == conversation.KIND_CODE
        and message.purpose == "Scale Cube 1.5x on Z"
        and message.detail == "pass"
        for message in bullet.messages
    ),
)
check(
    "and a tool row naming it, still running",
    bullet.running_tool is not None
    and bullet.running_tool.purpose == "Scale Cube 1.5x on Z"
    and bullet.running_tool.status == conversation.STATUS_RUNNING,
)
check("the status says code is running", bullet.status == "running code")
check("nothing has run yet", len(bullet.pending) == 1 and not sender.sent)

check("a tick runs the queued call", bullet.pump() is True)
check("and only that one", not bullet.pending)
check(
    "the row is finished and carries the output",
    bullet.running_tool is None
    and bullet._rows["c1"].status == conversation.STATUS_OK
    and bullet._rows["c1"].detail == '{\n  "ok": true\n}',
)
check(
    "the result is on the wire against the call's id",
    bullet.history[-1]
    == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
)
check("that tick did not also send a request", not sender.sent)

check("the next tick asks for the following round", bullet.pump() is True)
check("exactly one request went out", len(sender.sent) == 1)
check("the base prompt is still index 0", sender.sent[0][0] == {"role": "system", "content": "BASE"})
check(
    "the round carries the tool result the model asked for",
    sender.sent[0][-2] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
)
check(
    "and the live summary is still the last message",
    sender.sent[0][-1] == {"role": "system", "content": "LIVE SUMMARY"},
)
check("the turn is streaming again", bullet.phase == conversation.PHASE_REQUEST)
check("a new reply block was opened for it", bullet.messages[-1].kind == conversation.KIND_ASSISTANT)
check("a tick with a reply in flight does nothing", bullet.pump() is False)

deliver(bullet, "Done: the cube is 1.5x taller on Z.")
check("a prose reply with no calls ends the turn", not bullet.streaming)
check("and returns to idle", bullet.phase == conversation.PHASE_IDLE)
check(
    "the terminal reply is on the wire history",
    bullet.history[-1] == {"role": "assistant", "content": "Done: the cube is 1.5x taller on Z."},
)
check("nothing is left unanswered", unanswered(bullet.history) == [])
check("and no further request goes out", len(sender.sent) == 1)
check("the panel keeps one row per call, never removed", sum(1 for m in bullet.messages if m.kind == conversation.KIND_TOOL) == 1)


# A failing call: the full traceback is in the result, and the model has it on
# the very next round. This is the real sandbox behind the real loop.
print("\n-- a failing call --")

failing = conversation.Conversation()
failing_sender = FakeSender()
wire(
    failing,
    failing_sender,
    execute=lambda call: execution.execute_tool(call, lambda: {"bpy": SimpleNamespace()}),
)
failing.begin_turn("make it taller")
deliver(
    failing,
    calls=[wire_call("f1", "Scale Cube on Z", "raise RuntimeError('no active object')")],
)
failing.pump()
check(
    "a failing call is an error row with the traceback behind it",
    failing._rows["f1"].status == conversation.STATUS_ERROR
    and "Traceback" in failing._rows["f1"].detail,
)
check(
    "the wire result is the failure envelope",
    json.loads(failing.history[-1]["content"])["ok"] is False,
)
check("the sandbox got the model's code and not a wrapper", "no active object" in failing.history[-1]["content"])
failing.pump()
check("the model receives the traceback on the following round", len(failing_sender.sent) == 1)
check(
    "and the traceback is in that request, character for character",
    "RuntimeError: no active object" in failing_sender.sent[0][-2]["content"],
)
check("the loop did not retry it", len(failing_sender.sent) == 1)


# One call per tick, with a second one visibly waiting its turn.
print("\n-- one tool call per tick --")

pair = conversation.Conversation()
pair_sender = FakeSender()
wire(pair, pair_sender)
pair.begin_turn("two things")
deliver(
    pair,
    calls=[
        wire_call("p1", "First change", "pass"),
        wire_call("p2", "Second change", "pass"),
    ],
)
check("both calls are queued", len(pair.pending) == 2)
check("both rows exist before either runs", sum(1 for m in pair.messages if m.kind == conversation.KIND_TOOL) == 2)
pair.pump()
check("the first ran", pair._rows["p1"].status == conversation.STATUS_OK)
check(
    "the second is still the running row, so the panel can paint it",
    pair.running_tool is not None and pair.running_tool.call_id == "p2",
)
check("and the loop has not sent anything yet", not pair_sender.sent)
pair.pump()
check("the second ran on its own tick", pair._rows["p2"].status == conversation.STATUS_OK)
pair.pump()
check("only then does the next round go out", len(pair_sender.sent) == 1)


# Stop mid-turn: every call the model asked for must still be answered. This is
# the difference between a stopped turn and a conversation that can no longer be
# sent at all.
print("\n-- Stop mid-turn --")

stopped = conversation.Conversation()
stopped_sender = FakeSender()
wire(stopped, stopped_sender)
stopped.begin_turn("do three things")
deliver(
    stopped,
    calls=[
        wire_call("s1", "First", "pass"),
        wire_call("s2", "Second", "pass"),
        wire_call("s3", "Third", "pass"),
    ],
)
stopped.pump()
check("Stop is available while calls are queued", stopped.streaming)
check("Stop stops the turn", stopped.cancel() is True)
check("and nothing is still queued", not stopped.pending and stopped.phase == conversation.PHASE_IDLE)
check(
    "every call the model made has a result",
    unanswered(stopped.history) == [],
)
check(
    "the unrun calls are marked cancelled",
    json.loads(stopped.history[-1]["content"])["error"]["kind"] == "cancelled",
)
check(
    "their rows say so too, rather than pretending they ran",
    stopped._rows["s3"].status == conversation.STATUS_ERROR,
)
check("Stop sends nothing", not stopped_sender.sent)
check("a stopped turn can be sent again", stopped.begin_turn("continue") is None and stopped.streaming)

stopped2 = conversation.Conversation()
wire(stopped2, FakeSender())
stopped2.begin_turn("hello")
stopped2.apply_event({"ev": "delta", "text": "half a sentence"})
stopped2.cancel()
check(
    "a stopped stream commits what it said, so the history reads",
    stopped2.history[-1] == {"role": "assistant", "content": "half a sentence"},
)
stopped2.begin_turn("again")
check("and the next turn appends to a valid history", len(stopped2.history) == 3)


# ==========================================================================
# The budget (build ticket 07). Ticket 17 measured *which constructs* a wall-clock
# SIGALRM budget stops - a pure-Python loop, a sleep and a blocked read yes; a
# long native call only once it returns; a code path that swallows the raise or
# disarms the timer not at all. Those measurements are transcribed, not re-made
# here: what these checks cover is the *rule* built on them - the two clocks, the
# sentences, and what the loop does with a verdict.
#
# The checks that arm a real alarm use short numbers and short polls rather than
# the shipped 15 s / 60 s, because a check that sleeps for a minute is a check
# nobody runs. The shipped numbers are checked as copy, and the constructs are
# re-measured on the installed Blender by `tools/budget_probe.py`.
# ==========================================================================
print("\n-- the budget --")

running_copy = " ".join(budget.RUNNING_LINES)
check(
    "the panel's running copy names the per-call figure",
    f"{int(budget.CALL_SECONDS)}s" in running_copy,
)
check(
    "and the per-turn figure",
    f"{int(budget.TURN_SECONDS)}s" in running_copy,
)
check(
    "the shipped figures are ticket 17's, not a tune-up",
    (budget.CALL_SECONDS, budget.TURN_SECONDS) == (15.0, 60.0),
)
check(
    "the copy promises a pure-Python loop stops",
    "loop" in running_copy and "stops at the budget" in running_copy,
)
check(
    "it does not claim a native call stops once it has started",
    "only stops once it returns" in running_copy,
)
check(
    "it says Stop cannot be delivered while the code runs",
    "cannot be delivered" in running_copy,
)
check(
    "and it says what cannot be stopped at all",
    "swallows the interrupt" in running_copy and "switches the budget off" in running_copy,
)

_previous_handler = signal.getsignal(signal.SIGALRM)

# A pure-Python infinite loop. The case ticket 17 measured as stoppable, and the
# one this ticket is named after.
spinner = budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=0.05)
spins = 0
started = time.monotonic()
try:
    with spinner.call():
        while True:
            spins += 1
except budget.Exceeded as caught:
    loop_stop = caught
else:  # pragma: no cover - the failure being tested for
    loop_stop = None
loop_seconds = time.monotonic() - started
check("a plain infinite loop is interrupted at all", loop_stop is not None and spins > 1_000)
check("at its own budget rather than after it", 0.1 <= loop_seconds < 0.6)
check("and the interrupt says which budget it was", loop_stop.kind == budget.KIND_CALL)
check(
    "the timer is off again once the call is over",
    signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0),
)
check(
    "and the handler the session had before is back",
    signal.getsignal(signal.SIGALRM) is _previous_handler,
)

# A blocking call that *is* stoppable - ticket 17 measured `time.sleep` and a
# blocking `recv` at 1.00 s, which is why the panel's copy must not say "a
# blocking C call never stops" any more.
napper = budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=0.05)
started = time.monotonic()
try:
    with napper.call():
        time.sleep(5)
except budget.Exceeded:
    slept = True
else:  # pragma: no cover - the failure being tested for
    slept = False
check("a sleep is interrupted too, not waited out", slept and time.monotonic() - started < 1.0)

# The swallow case, bounded so the check terminates: the first interrupt is
# caught, and the *next* one - one tick later - is not, because it lands outside
# the `try`. That is the whole reason the alarm repeats.
greedy = budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=0.05)
swallowed = []
try:
    with greedy.call():
        try:
            while True:
                pass
        except budget.Exceeded:
            swallowed.append("caught")
        while True:
            pass
except budget.Exceeded as caught_again:
    second_stop = caught_again
else:  # pragma: no cover - the failure being tested for
    second_stop = None
check(
    "code that catches the interrupt is interrupted again",
    swallowed == ["caught"] and second_stop is not None,
)
check("so catching it buys one tick, not the rest of the call", second_stop.interrupts >= 2)

# Disarming. Both ways to switch the budget off that ticket 17 measured, and the
# answer is the same in both: notice it, and say so, because it cannot be
# prevented from inside the process.
thief = budget.Limits(call_seconds=5.0, turn_seconds=60.0, poll=0.05)
with thief.call():
    signal.setitimer(signal.ITIMER_REAL, 0)
check("code that switches the timer off is noticed", thief.verdict()["disarmed"] is True)
check("and the verdict does not pretend it was interrupted", thief.verdict()["interrupted"] is False)
replacer = budget.Limits(call_seconds=5.0, turn_seconds=60.0, poll=0.05)
with replacer.call():
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
check("so is code that replaces the handler", replacer.verdict()["disarmed"] is True)
check("and the handler is restored afterwards", signal.getsignal(signal.SIGALRM) is _previous_handler)

# The turn's clock, cumulative across calls. The call's own limit is far away, so
# the only thing that can end this sleep is the turn's.
capped = budget.Limits(call_seconds=10.0, turn_seconds=0.15, poll=0.05)
capped.begin_turn()
try:
    with capped.call():
        time.sleep(5)
except budget.Exceeded as caught_by_turn:
    turn_stop = caught_by_turn
else:  # pragma: no cover - the failure being tested for
    turn_stop = None
check("the turn's cumulative budget ends a call early", turn_stop is not None)
check("and it says so, rather than blaming the call", turn_stop.kind == budget.KIND_TURN)
check("against the turn's remaining seconds", turn_stop.limit <= 0.16)
capped.end_turn()
check("the turn's clock stops with the turn", capped.turn_open is False)
check("the next turn starts it again", capped.begin_turn() is True and capped.turn_open)

# What the turn's ledger actually counts. It used to be wall clock from the turn's
# start, which charged the model's *thinking* - and every HTTP round trip - to a
# budget whose purpose is to bound model-authored code. Two live runs measured the
# difference rather than the principle (2026-09-26): 67 s and 79 s turns whose code
# added up to a few seconds were refused the call that would have finished the job,
# and the refusal said "this turn's budget for running code is used up" about a turn
# that had barely run any. The documented figure is "60 s cumulative per user turn"
# of the seconds the alarm can see, so the ledger is the call windows.
_tick = [0.0]


def fake_clock() -> float:
    return _tick[0]


ledger = budget.Limits(call_seconds=10.0, turn_seconds=60.0, poll=1.0, clock=fake_clock)
ledger.begin_turn()
_tick[0] += 100.0
check(
    "a turn that spends 100s thinking has spent nothing of its code budget",
    ledger.turn_elapsed == 0.0 and ledger.allowance() == (10.0, budget.KIND_CALL),
)
with ledger.call():
    _tick[0] += 6.0
check("but the seconds code runs are charged", abs(ledger.turn_elapsed - 6.0) < 0.01)
with ledger.call():
    _tick[0] += 55.0
check("across calls, not just the first", abs(ledger.turn_elapsed - 61.0) < 0.01)
_tick[0] += 30.0
# `except ... as name` unbinds `name` when the block ends, so the exception is
# copied out inside the block - the same shape the other budget checks here use,
# and the reason the first version of this one raised `NameError` instead of
# reporting anything about the budget.
turn_refusal = None
try:
    with ledger.call():
        third_ran = True
except budget.Exceeded as caught_by_ledger:
    turn_refusal = caught_by_ledger
    third_ran = False
check(
    "so the next call is refused once the code has added up past the budget",
    third_ran is False
    and turn_refusal is not None
    and turn_refusal.kind == budget.KIND_TURN
    and ledger.allowance()[0] <= 0.0,
)
ledger.end_turn()
check("and a new turn starts with a full budget", ledger.begin_turn() and ledger.turn_elapsed == 0.0)

# A turn whose budget is already spent refuses the next call *before it runs*, and
# the loop stops the turn on that refusal. This is the one path where the guard is
# never armed - there is no window to arm - so a "was this a stop?" test that
# demanded a live alarm would silently ignore it and let the loop collect refusals
# up to the caps instead. That is a hole this check closed rather than a case it
# documents: the first version of `budget.stopped` required `armed`, and this is
# the check that failed.
spent = budget.Limits(call_seconds=10.0, turn_seconds=0.3, poll=0.05)
spent.begin_turn()
try:
    with spent.call():
        time.sleep(5)
except budget.Exceeded:
    pass
refused = execution.run_python("print('never runs')", "One more call", {}, limits=spent)
check("a call that starts after the turn's budget is gone is refused", refused["ok"] is False)
check(
    "and the envelope names the budget",
    refused["envelope"]["error"]["kind"] == "budget"
    and refused["envelope"]["error"]["budget_kind"] == budget.KIND_TURN,
)
check("the code never ran", "never runs" not in (refused["envelope"]["stdout"] or ""))
check(
    "and the model is told nothing ran, rather than that something was interrupted",
    "No code ran" in (refused["envelope"]["note"] or ""),
)
check(
    "and the loop reads that refusal as a stop",
    conversation._budget_verdict(refused) != {},
)
# The sentence the refusal reaches the panel and the model with. A live run
# produced "7.27s of -7.27s" here (2026-09-26), because the limit is the *negative*
# remainder for a call refused at the door - a sentinel `_record` needs, and a
# number nobody should be shown.
check(
    "a refusal at the door does not print a negative budget",
    "s of -" not in refused["envelope"]["error"]["message"]
    and "already spent" in refused["envelope"]["error"]["message"],
)
check(
    "while an overrun mid-call still reports spent-of-limit",
    "of 60.00s" in budget.Exceeded(budget.KIND_TURN, 61.5, 60.0).args[0],
)
spent_turn = conversation.Conversation()
wire(spent_turn, FakeSender(), execute=lambda call: refused)
spent_turn.begin_turn("one more call")
deliver(spent_turn, calls=[wire_call("sp1", "One more call", "print('never runs')")])
spent_turn.pump()
check("so a refused call ends the turn too", not spent_turn.streaming)
check("and the panel says which budget was gone", "call time" in spent_turn.messages[-1].text)

# The same, through the sandbox: what the model's code gets told about it.
limit_stop = execution.run_python(
    "print('before')\nwhile True:\n    pass",
    "Loop forever",
    {},
    limits=budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=0.05),
)
check("the sandbox reports the interrupt instead of raising it", limit_stop["ok"] is False)
check(
    "the envelope calls it a budget stop, not a traceback",
    limit_stop["envelope"]["error"]["kind"] == "budget",
)
check(
    "and says how long the code actually ran",
    0.1 <= limit_stop["envelope"]["error"]["seconds"] < 1.0,
)
check(
    "what it printed before the interrupt is kept",
    limit_stop["envelope"]["stdout"].strip() == "before",
)
check(
    "the model is told the call did not finish",
    "budget" in (limit_stop["envelope"]["note"] or ""),
)
check("the wire content is a valid envelope", json.loads(limit_stop["content"])["ok"] is False)

# And the swallow path through the sandbox: the call *returns*, so it is not an
# error, but it is not a clean call either. The poll is long and the budget short
# on purpose - the interrupt is delivered once, inside the `try`, and the code is
# back before the next tick can fire.
limit_swallow = execution.run_python(
    "try:\n"
    "    while True:\n"
    "        pass\n"
    "except BaseException:\n"
    "    print('caught it')\n",
    "Swallow the interrupt",
    {},
    limits=budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=2.0),
)
check("code that swallows the interrupt still returns a result", limit_swallow["ok"] is True)
check(
    "but the result says an interrupt was delivered",
    limit_swallow["envelope"]["budget"]["interrupts"] >= 1,
)
check(
    "and it is not reported as a clean call",
    "caught" in (limit_swallow["envelope"]["note"] or ""),
)
check("the code's own output is still there", "caught it" in limit_swallow["envelope"]["stdout"])

# The loop's half: a call that ran past its budget ends the turn, answers the
# call it was running, and sends no further request. What the loop reads is the
# envelope, which is why a fake executor can stand in for Blender here.
def budget_verdict(**over):
    verdict = {
        "kind": budget.KIND_CALL,
        "seconds": 0.2,
        "limit": budget.CALL_SECONDS,
        "interrupts": 1,
        "interrupted": True,
        "late": False,
        "disarmed": False,
        "armed": True,
    }
    verdict.update(over)
    return verdict


class Recorder:
    """The undo seam, counted. Same protocol as `undo_blender`."""

    def __init__(self):
        self.opened: list[str] = []
        self.closed = 0

    def open_turn(self, text: str) -> bool:
        self.opened.append(text)
        return True

    def close_turn(self) -> bool:
        self.closed += 1
        return True


stopped_running = conversation.Conversation()
stopped_running_undo = Recorder()
stopped_running_sender = FakeSender()
wire(
    stopped_running,
    stopped_running_sender,
    execute=lambda call: execution.run_python(
        "while True:\n    pass",
        "Loop forever",
        {},
        limits=budget.Limits(call_seconds=0.15, turn_seconds=5.0, poll=0.05),
    ),
    undo=stopped_running_undo,
)
stopped_running.begin_turn("make it spin")
deliver(stopped_running, calls=[wire_call("z1", "Loop forever", "while True:\n    pass")])
stopped_running.pump()
check(
    "the interrupted call's row is an error, not a running row",
    stopped_running._rows["z1"].status == conversation.STATUS_ERROR,
)
check(
    "the turn ended rather than asking for another round",
    not stopped_running.streaming and stopped_running.phase == conversation.PHASE_IDLE,
)
check(
    "and no second request went out",
    not stopped_running_sender.sent,
)
check(
    "the interrupted call is answered, so history stays sendable",
    unanswered(stopped_running.history) == [],
)
check(
    "the panel says the code ran past its budget",
    "past its budget" in stopped_running.messages[-1].text,
)
check(
    "and it does not claim the turn was cleaned up",
    stopped_running.messages[-1].kind == conversation.KIND_ERROR,
)
check(
    "an interrupted turn still closes its undo record, so Ctrl+Z has a step",
    stopped_running_undo.closed == 1,
)

# The turn's clock is the session's, not a second one built per call: the loop
# opens it when a turn starts and closes it when the turn ends, whatever ended it.
clocked = conversation.Conversation()
clocked.limits = budget.Limits(call_seconds=15.0, turn_seconds=60.0)
wire(clocked, FakeSender())
clocked.begin_turn("open the clock")
check("a turn opens the budget's turn clock", clocked.limits.turn_open is True)
clocked.cancel()
check("and ending the turn closes it", clocked.limits.turn_open is False)


# Code that caught the interrupt: the call returned, so nothing was interrupted -
# but the budget did not bound it, and the loop stops the turn rather than buying
# the model another round with a mechanism it has shown it can ignore.
lax = conversation.Conversation()
lax_undo = Recorder()
wire(
    lax,
    FakeSender(),
    execute=lambda call: {
        "ok": True,
        "envelope": {"ok": True, "budget": budget_verdict(interrupted=False)},
        "content": '{"ok": true}',
        "detail": '{\n  "ok": true\n}',
        "summary": "caught it",
    },
    undo=lax_undo,
)
lax.begin_turn("try to catch it")
deliver(lax, calls=[wire_call("w1", "Swallow the interrupt", "pass")])
lax.pump()
check("a swallowed interrupt still ends the turn", not lax.streaming)
check("and the panel names what happened", "caught the interrupt" in lax.messages[-1].text)
check("with the undo step still written", lax_undo.closed == 1)

thief_turn = conversation.Conversation()
wire(
    thief_turn,
    FakeSender(),
    execute=lambda call: {
        "ok": True,
        "envelope": {
            "ok": True,
            "budget": budget_verdict(interrupted=False, interrupts=0, disarmed=True),
        },
        "content": '{"ok": true}',
        "detail": '{\n  "ok": true\n}',
        "summary": "switched the timer off",
    },
)
thief_turn.begin_turn("switch it off")
deliver(thief_turn, calls=[wire_call("v1", "Disarm", "pass")])
thief_turn.pump()
check("code that switched the budget off ends the turn too", not thief_turn.streaming)
check(
    "and the panel says the budget was switched off, not that it fired",
    "switched the budget off" in thief_turn.messages[-1].text,
)

# A clean call is still a clean call: the guard must not turn every turn into a
# stop.
clean = conversation.Conversation()
clean_sender = FakeSender()
wire(clean, clean_sender, execute=lambda call: execution.run_python("print('fine')", "Fine", {}))
clean.begin_turn("just look")
deliver(clean, calls=[wire_call("n1", "Fine", "print('fine')")])
clean.pump()
check(
    "a call inside its budget is not stopped",
    clean.streaming and clean.phase == conversation.PHASE_TOOL,
)
check("and its row is ok", clean._rows["n1"].status == conversation.STATUS_OK)
clean.pump()
check("so the loop does ask for the next round", len(clean_sender.sent) == 1)
check("and the turn is streaming again", clean.phase == conversation.PHASE_REQUEST)


# The caps. Each one stops the turn AND says so.
print("\n-- the caps --")

rounds = conversation.Conversation()
rounds_sender = FakeSender()
wire(rounds, rounds_sender)
rounds.begin_turn("loop forever")
spins = 0
while rounds.streaming and spins < 40:
    spins += 1
    deliver(rounds, calls=[wire_call(f"r{spins}", f"Change {spins}", "pass")])
    rounds.pump()
    rounds.pump()
check("the round cap stops a converging loop", spins < 40 and not rounds.streaming)
check("after exactly the declared rounds", len(rounds_sender.sent) == conversation.MAX_ROUNDS - 1)
check("and it names the cap rather than failing silently", "8 rounds" in rounds.messages[-1].text)
check("the cap leaves an error block, not an unwinding", rounds.messages[-1].kind == conversation.KIND_ERROR)
check("history stays sendable after a cap", unanswered(rounds.history) == [])

repeated = conversation.Conversation()
wire(repeated, FakeSender())
ran = []
repeated.execute = lambda call: (ran.append(call), ok_executor(call))[1]
repeated.begin_turn("again and again")
deliver(
    repeated,
    calls=[wire_call("t1", "Same thing", "pass"),
           wire_call("t2", "Same thing", "pass"),
           wire_call("t3", "Same thing", "pass")],
)
for _ in range(3):
    repeated.pump()
check("the same call is not run a third time", len(ran) == 2)
check("the repeat stops the turn", not repeated.streaming)
check("and the third call is answered, not orphaned", unanswered(repeated.history) == [])
check("the row for it says it did not run", repeated._rows["t3"].status == conversation.STATUS_ERROR)

many = conversation.Conversation()
wire(many, FakeSender())
many_ran = []
many.execute = lambda call: (many_ran.append(call), ok_executor(call))[1]
many.begin_turn("thirty things")
deliver(
    many,
    calls=[wire_call(f"m{i}", f"Change {i}", "pass") for i in range(30)],
)
for _ in range(30):
    many.pump()
check("the call cap bounds one round of parallel calls", len(many_ran) == conversation.MAX_TOOL_CALLS)
check("and stops the turn", not many.streaming)
check("naming the limit", "24-call" in many.messages[-1].text)
check("with every leftover call answered", unanswered(many.history) == [])

failing_rounds = conversation.Conversation()
failing_sender = FakeSender()
wire(failing_rounds, failing_sender, execute=fail_executor)
failing_rounds.begin_turn("try and fail")
fails = 0
while failing_rounds.streaming and fails < 10:
    fails += 1
    deliver(failing_rounds, calls=[wire_call(f"x{fails}", f"Attempt {fails}", "pass")])
    failing_rounds.pump()
    failing_rounds.pump()
check("three all-failing rounds stop the turn", fails == conversation.FAIL_STREAK_LIMIT)
check("and it says which cap did it", "every call" in failing_rounds.messages[-1].text)


# Malformed arguments are a continuation, not a retry: the model gets one cheap
# round to fix itself, and the loop does not do the fixing.
print("\n-- malformed arguments --")

malformed = conversation.Conversation()
malformed_sender = FakeSender()
# The real tool layer, so the `tool_argument_error` under test is the one that
# ships rather than a fake that agrees with the test.
wire(malformed, malformed_sender, execute=lambda call: execution.execute_tool(call, {}))
malformed.begin_turn("break it")
deliver(
    malformed,
    calls=[{"id": "b1", "function": {"name": execution.RUN_PYTHON, "arguments": "{oops"}}],
)
malformed.pump()
check(
    "a malformed call is fed back with its raw text",
    json.loads(malformed.history[-1]["content"])["error"]["kind"] == "tool_argument_error"
    and "oops" in malformed.history[-1]["content"],
)
check("the turn did not abort", malformed.streaming)
malformed.pump()
check("the model is asked to continue, not to repeat", len(malformed_sender.sent) == 1)
check(
    "and the bad call is in that request",
    "tool_argument_error" in malformed_sender.sent[0][-2]["content"],
)


# A reply truncated at the token limit leaves a call that is incomplete, not
# wrong, so it is never half-run and never half-committed.
print("\n-- a truncated reply --")

truncated = conversation.Conversation()
truncated_sender = FakeSender()
wire(truncated, truncated_sender)
truncated.begin_turn("write an essay")
deliver(truncated, calls=[wire_call("k1", "Half a call", "pass")], reason="length")
check("a length-truncated reply is terminal", not truncated.streaming)
check("its calls never enter history", unanswered(truncated.history) == [] and not truncated.pending)
check("and none of them run", truncated.pump() is False)
check("the user is told the reply was cut off", "token limit" in truncated.messages[-1].text)
check("and nothing was sent after it", not truncated_sender.sent)


# The summary is recomputed every round, and the base prompt stays at index 0.
print("\n-- the request shape --")

shape = conversation.Conversation()
shape_sender = FakeSender()
seen = []


def moving_context():
    seen.append(1)
    return "BASE", f"LIVE SUMMARY {len(seen)}"


shape.attach(send=shape_sender, execute=ok_executor, context=moving_context)
shape.begin_turn("two rounds")
deliver(shape, calls=[wire_call("g1", "First", "pass")])
shape.pump()
shape.pump()
check("the context hook is asked once per request", len(seen) == 1)
check(
    "so the trailing summary is the one captured for THAT request",
    shape_sender.sent[0][-1]["content"] == "LIVE SUMMARY 1",
)
check("and the base prompt is byte-identical", shape_sender.sent[0][0]["content"] == "BASE")

# ------------------------------------------------------------------ persistence
#
# The store keeps **one** schema - the provider's - so reopening a file re-derives
# the display rows from the wire history instead of reading a second, lossier one.
# These checks are about that derivation, and about what happens to a turn that is
# still in flight when the file underneath it changes.
print("\n-- what survives a restart --")

RESTORED_TURN = [
    {"role": "user", "content": "make it taller"},
    {
        "role": "assistant",
        "content": "Scaling it now.",
        "tool_calls": [
            {
                "id": "p1",
                "type": "function",
                "function": {
                    "name": "run_blender_python",
                    # A JSON *string*, as the provider sends it (measured, ticket 16).
                    "arguments": json.dumps({"code": "obj.scale.z = 2", "purpose": "Scale on Z"}),
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "p1",
        "content": json.dumps(
            {"ok": True, "tool": "run_blender_python", "summary": "done", "output": "z scale 2.00"}
        ),
    },
    {"role": "assistant", "content": "Done - the cube is twice as tall."},
]

rebuilt = conversation.messages_from_history(RESTORED_TURN)
check("the ask comes back", rebuilt[0].role == "user" and rebuilt[0].text == "make it taller")
check("the assistant's prose comes back", rebuilt[1].text == "Scaling it now.")
check(
    "the code row comes back with its identity",
    rebuilt[2].kind == conversation.KIND_CODE and rebuilt[2].purpose == "Scale on Z",
)
check("and with the code itself, for `Show code`", rebuilt[2].detail == "obj.scale.z = 2")
check(
    "the tool row comes back named",
    rebuilt[3].kind == conversation.KIND_TOOL and rebuilt[3].purpose == "Scale on Z",
)
check("with its status read from the envelope", rebuilt[3].status == conversation.STATUS_OK)
check(
    "its output behind the expander, indented to read",
    '"output"' in rebuilt[3].detail and "\n" in rebuilt[3].detail,
)
check("and the closing prose", rebuilt[4].text == "Done - the cube is twice as tall.")
check("no row is invented", len(rebuilt) == 5)
check("every restored call row names the call it reports", rebuilt[3].call_id == "p1")
check(
    "nothing restored is left mid-stream",
    all(message.status != conversation.STATUS_RUNNING for message in rebuilt),
)

RESTORED_FAILURE = [
    {"role": "user", "content": "break it"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "p2",
                "function": {
                    "name": "run_blender_python",
                    "arguments": json.dumps({"code": "raise", "purpose": "Break it"}),
                },
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "p2",
        "content": json.dumps({"ok": False, "error": {"kind": "runtime_error", "message": "boom"}}),
    },
]
failed_rows = conversation.messages_from_history(RESTORED_FAILURE)
check("a failed call comes back as a failure", failed_rows[-1].status == conversation.STATUS_ERROR)
check(
    "its error text is behind the expander, not in the row",
    "boom" in failed_rows[-1].detail and "boom" not in failed_rows[-1].text,
)
check(
    "an assistant message of `None` content is not drawn as an empty reply",
    len(failed_rows) == 3 and not any(row.kind == conversation.KIND_ASSISTANT for row in failed_rows),
)

ORPHANED_CALL = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "p3", "function": {"name": "t", "arguments": "{}"}}],
    }
]
orphan_rows = conversation.messages_from_history(ORPHANED_CALL)
check(
    "a stored call with no stored result never looks like it is still running",
    orphan_rows[0].status == conversation.STATUS_ERROR,
)
check("and says the result is missing", "no result" in orphan_rows[0].detail.lower())

restored = conversation.Conversation()
restored.begin_turn("something else entirely")
restored.apply_event({"ev": "delta", "text": "nope"})
restored.apply_event({"ev": "done", "finish_reason": "stop"})
restored.restore(RESTORED_TURN)
check("restore puts the stored history on the wire transcript", restored.history == RESTORED_TURN)
check("and on the display one, replacing what was there", restored.messages[0].text == "make it taller")
check("with nothing in flight", not restored.streaming and restored.phase == conversation.PHASE_IDLE)
check("and the conversation can be sent again", restored.begin_turn("carry on") is None and restored.streaming)
check("snapshot copies, so a caller cannot reach back into the transcript", restored.snapshot()[0] is not restored.history[0])
check("while carrying the same messages", restored.snapshot()[:2] == RESTORED_TURN[:2])
restored.cancel()

# A file switch mid-turn. Nothing of the abandoned turn may reach the new
# conversation, and the old one must stay sendable for when the user returns.
print("\n-- switching files mid-turn --")

switching = conversation.Conversation()
switching_sender = FakeSender()
wire(switching, switching_sender)
switching.begin_turn("do two things")
deliver(switching, calls=[wire_call("q1", "One", "pass"), wire_call("q2", "Two", "pass")])
switching.pump()
started_in = switching.generation
check("a turn is in the generation it started in", started_in == switching.turn_generation)
check("closing the file abandons it", switching.abandon() is True)
check("the turn is over", not switching.streaming and switching.phase == conversation.PHASE_IDLE)
check("the queued call is answered rather than orphaned", unanswered(switching.history) == [])
check(
    "and its answer names the reason it never ran",
    json.loads(switching.history[-1]["content"])["error"]["kind"] == "scope_changed",
)
check("the generation moved on", switching.generation > started_in)
check("a late chunk for the old turn is dropped", switching.apply_event({"ev": "delta", "text": "late"}) is False)
check(
    "and so is a fatal one, which would otherwise blame the new file",
    switching.apply_event(
        {"ev": "startup_failed", "fatal": True, "message": "The worker exited"}
    )
    is False
    and switching.transport_error is None,
)
check("nothing of it is on screen", "late" not in "".join(m.text for m in switching.messages))
check("the next turn makes the session live again", switching.begin_turn("fresh") is None)
check("and re-syncs the generation", switching.generation == switching.turn_generation)
check("so its events land", switching.apply_event({"ev": "delta", "text": "hello"}) is True)
switching.cancel()

idle = conversation.Conversation()
check("abandoning an idle session is not an event", idle.abandon() is False)
check(
    "but it still fences the generation, so a stray fatal event cannot invent a banner",
    idle.apply_event({"ev": "startup_failed", "fatal": True, "message": "boom"}) is False
    and idle.transport_error is None,
)


# ==========================================================================
# The undo step and the receipt (build ticket 05). The rule is bpy-free - the
# label, the mutation diff, the exact sentences and the pause decision are all
# decided in `undo.py` - so all of it is checked here. Only the push itself needs
# Blender, and that is `tools/undo_step_probe.py`'s job.
# ==========================================================================
print("\n-- the turn's undo record --")


class FakeUndo:
    """The undo seam, recorded. `undo_blender` is what it stands in for.

    The loop must never learn what a `undo_push` is, so what is checkable here is
    exactly the protocol: one open per turn with the user's own words, and one
    close per turn however the turn ends.
    """

    def __init__(self):
        self.opened: list[str] = []
        self.closed = 0

    def open_turn(self, text: str) -> bool:
        self.opened.append(text)
        return True

    def close_turn(self) -> bool:
        self.closed += 1
        return True


class AngryEnd(conversation.Conversation):
    """A conversation whose `_end_turn` cannot finish its own bookkeeping.

    `close_turn` sits in a `finally` because a path that dies halfway through
    ending a turn must still leave the user a Ctrl+Z (ticket 12 §1). This is the
    only way on plain CPython to make that path happen: `_end_turn`'s own body
    raises, and the step still has to be written.
    """

    def __init__(self):
        self._bomb = False
        super().__init__()

    @property
    def pending(self):
        return self._pending

    @pending.setter
    def pending(self, value):
        if self._bomb:
            raise RuntimeError("the turn ends badly")
        self._pending = value


records = FakeUndo()
undone = wire(conversation.Conversation(), FakeSender(), undo=records)
undone.begin_turn("  make it taller  ")
check("a turn opens the undo record", records.opened == ["make it taller"])
check("and nothing has closed yet", records.closed == 0)
deliver(undone, text="done")
check("a finished turn closes it, once", records.closed == 1)
deliver(undone, text="again")
check("a second reply does not close it again", records.closed == 1)
undone.begin_turn("stop this one")
undone.cancel()
check("Stop closes it too", records.closed == 2)
undone.begin_turn("and this one")
undone.fail_turn("worker_unavailable", "the worker died")
check("a transport failure closes it", records.closed == 3)
undone.begin_turn("truncated")
deliver(undone, reason="length")
check("a truncated reply closes it", records.closed == 4)
check(
    "and every open belongs to its own turn",
    records.opened == ["make it taller", "stop this one", "and this one", "truncated"],
)

angry = AngryEnd()
angry_undo = FakeUndo()
wire(angry, FakeSender(), undo=angry_undo)
angry.begin_turn("end badly")
angry._bomb = True
try:
    angry._end_turn()
    exploded = False
except RuntimeError:
    exploded = True
check(
    "a turn that dies halfway through ending still leaves its undo step",
    exploded and angry_undo.closed == 1,
)


print("\n-- the undo rule --")


def summary(**over):
    """A `get_scene_info` summary object, as `execution.scene_summary` builds it."""
    fields = {
        "scene": "Scene",
        "filepath": "/tmp/scene.blend",
        "is_saved": True,
        "is_dirty": False,
        "mode": "OBJECT",
        "frame": 1,
        "frame_range": [1, 250],
        "render_engine": "BLENDER_EEVEE_NEXT",
        "unit_system": "METRIC",
        "global_undo": True,
        "object_count": 3,
        "collection_tree": [{"name": "Collection", "count": 3, "children": []}],
        "collection_tree_truncated": False,
        "object_type_counts": {"MESH": 1, "CAMERA": 1, "LIGHT": 1},
        "selection": {"count": 1, "names": ["Cube"]},
        "active": {"name": "Cube", "type": "MESH"},
        "captured": "turn_start",
        "schema": 1,
    }
    fields.update(over)
    return fields


# -- the label: the step has to name the turn it belongs to ----------------
check(
    "the step is labelled with the turn",
    undo.label("make the cube taller") == "copilot: make the cube taller",
)
check(
    "a label is one line, whatever shape the turn had",
    undo.label("  make\n it   taller  ") == "copilot: make it taller",
)
LONG_TURN = "please make the cube taller on the z axis and then tell me about it"
long_label = undo.label(LONG_TURN)
check(
    "a long turn is cut to the label budget, ellipsis included",
    len(long_label) <= len(undo.LABEL_PREFIX) + undo.LABEL_CHARS + 1,
)
check(
    "and it is cut at a word, not mid-word",
    long_label == "copilot: please make the cube taller on the z\u2026",
)
check("a blank turn still labels its step", undo.label("") == "copilot: agent turn")
check(
    "the baseline marker is a marker, not a turn",
    undo.BASELINE_LABEL.startswith(undo.LABEL_PREFIX) and undo.BASELINE_LABEL != undo.label(""),
)

# -- what a turn changed, from the bounded summary -------------------------
check("a turn that changed nothing has nothing to name", undo.changes(summary(), summary()) == [])
check(
    "and the capture's own stamp is not a change",
    undo.changes(summary(), summary(captured="turn_end")) == [],
)
check(
    "an added object is named, with the object count",
    undo.changes(
        summary(),
        summary(object_count=4, object_type_counts={"MESH": 2, "CAMERA": 1, "LIGHT": 1}),
    )
    == ["objects: 3 \u2192 4", "MESH: 1 \u2192 2"],
)
check(
    "a removed object is named too",
    undo.changes(
        summary(),
        summary(object_count=2, object_type_counts={"MESH": 1, "CAMERA": 1}),
    )
    == ["objects: 3 \u2192 2", "LIGHT: 1 \u2192 none"],
)
check(
    "a new active object is named",
    undo.changes(summary(), summary(active={"name": "Camera", "type": "CAMERA"}))
    == ["active: Cube \u2192 Camera"],
)
check("a selection is named", undo.changes(summary(), summary(selection={"count": 3, "names": []})) == ["selection: 1 \u2192 3"])
check("a mode change is named", undo.changes(summary(), summary(mode="EDIT_MESH")) == ["mode: OBJECT \u2192 EDIT_MESH"])
check(
    "a different file is named, because that is a save",
    undo.changes(summary(), summary(filepath="/tmp/other.blend")) == ["file: /tmp/scene.blend \u2192 /tmp/other.blend"],
)
check(
    "a collection change is named without pretending to be precise",
    undo.changes(
        summary(),
        summary(collection_tree=[{"name": "Targets", "count": 1, "children": []}]),
    )
    == ["collections changed"],
)
check(
    "a frame change is named",
    undo.changes(summary(), summary(frame=20)) == ["frame: 1 \u2192 20"],
)
BUSY_AFTER = dict(
    object_count=9,
    object_type_counts={"MESH": 8},
    mode="EDIT_MESH",
    frame=20,
    scene="Scene.001",
    global_undo=False,
)
busy_diff = undo.changes(summary(), summary(**BUSY_AFTER))
check("a turn that did five things produces five lines", len(busy_diff) > undo.RECEIPT_MAX_LINES)

# -- the receipt the panel shows -------------------------------------------
pushed = undo.receipt(
    summary(),
    summary(object_count=4),
    pushed=True,
    label_text="copilot: add a cube",
)
check("a pushed turn is undoable", pushed["undoable"] is True and pushed["title"] == "Undoable")
check("the receipt names what changed", pushed["changed"] == ["objects: 3 \u2192 4"])
check("the receipt carries the step's label", pushed["label"] == "copilot: add a cube")
check(
    "and it says one Ctrl+Z takes the turn back",
    any("Ctrl+Z reverts this turn" in line for line in pushed["lines"]),
)
check(
    "it names the shortcut for Blender's undo history",
    any("Ctrl+Alt+Z" in line for line in pushed["lines"]),
)
check(
    "and it is honest about what undo does not cover",
    any("files, network" in line for line in pushed["lines"]),
)
check("with nothing dropped, it says so", pushed["more"] == 0)

many = undo.receipt(
    summary(), summary(**BUSY_AFTER), pushed=True, label_text="copilot: do five things"
)
check(
    "a busy turn is named, but the receipt stays short",
    len(many["changed"]) == undo.RECEIPT_MAX_LINES,
)
check(
    "and it reports how many lines it did not draw",
    many["more"] == len(busy_diff) - undo.RECEIPT_MAX_LINES,
)

unnamed = undo.receipt(
    # Identical summaries: the depsgraph flag is what pushed this turn, and a
    # scale on an existing object is exactly the change the bounded summary
    # cannot see (it is datablock-level, not property-level).
    summary(),
    summary(),
    pushed=True,
    label_text="copilot: make it taller",
)
check(
    "a change the bounded summary cannot see is still admitted, not invented",
    unnamed["changed"] == [undo.UNNAMED_CHANGE],
)
check(
    "and the receiver of that is told it is not a property list",
    "data changed" in unnamed["changed"][0],
)

refused = undo.receipt(
    summary(mode="EDIT_MESH"),
    summary(mode="EDIT_MESH"),
    pushed=False,
    reason=undo.REFUSED_EDIT_MODE,
    label_text="copilot: build a box",
)
check("a refused step is not undoable", refused["undoable"] is False and refused["title"] == "Not undoable")
check(
    "the receipt says why, in the terms the measurement used",
    any("edit mode" in line for line in refused["lines"]),
)
check(
    "and it does not claim Ctrl+Z reverts this turn",
    not any("Ctrl+Z reverts this turn" in line for line in refused["lines"]),
)
check(
    "but the coverage sentence is still there, so the promise never grows",
    any("files, network" in line for line in refused["lines"]),
)
check(
    "a refused step has no changes to name beyond what the diff saw",
    refused["changed"] == [],
)

saved = undo.receipt(
    summary(),
    summary(filepath="/tmp/other.blend", is_saved=True),
    pushed=True,
    label_text="copilot: save it somewhere else",
)
check(
    "an outside-the-blend effect that IS detectable is named, not generally warned about",
    any("undo does not cover" in line for line in saved["lines"]),
)
check(
    "and that line says what the effect was",
    any("saved or renamed" in line for line in saved["lines"]),
)
check(
    "an ordinary turn gets no such line",
    not any("undo does not cover" in line for line in pushed["lines"]),
)

failed = undo.receipt(
    summary(),
    summary(object_count=4),
    pushed=False,
    reason=undo.REFUSED_PUSH_FAILED,
    detail="RuntimeError: Operator bpy.ops.ed.undo_push.poll() failed",
    label_text="copilot: add a cube",
)
check(
    "a push Blender refused says so with Blender's own words",
    any("poll() failed" in line for line in failed["lines"]),
)
check("and it is still not undoable", failed["undoable"] is False)

# -- when auto-run must pause ---------------------------------------------
check("Global Undo off pauses auto-run", undo.pause_reason(False, "OBJECT") == undo.PAUSE_GLOBAL_UNDO)
check("edit mode pauses it too", undo.pause_reason(True, "EDIT_MESH") == undo.PAUSE_EDIT_MODE)
check("so does any other edit mode", undo.pause_reason(True, "EDIT_CURVE") == undo.PAUSE_EDIT_MODE)
check(
    "sculpt mode does not: memfile undo is compatible there (ed_undo.cc:588)",
    undo.pause_reason(True, "SCULPT") is None,
)
check("object mode with Global Undo on runs", undo.pause_reason(True, "OBJECT") is None)
check(
    "the two pauses are distinguishable on screen",
    undo.PAUSE_LINES[undo.PAUSE_GLOBAL_UNDO] != undo.PAUSE_LINES[undo.PAUSE_EDIT_MODE],
)
check(
    "and each says auto-run is paused",
    all(
        any("paused" in line for line in undo.PAUSE_LINES[code])
        for code in (undo.PAUSE_GLOBAL_UNDO, undo.PAUSE_EDIT_MODE)
    ),
)
check(
    "the Global Undo banner still offers the fix",
    undo.PAUSE_ACTION[undo.PAUSE_GLOBAL_UNDO] == "blender_copilot.enable_global_undo",
)
check(
    "the edit-mode banner offers nothing but leaving edit mode",
    undo.PAUSE_ACTION[undo.PAUSE_EDIT_MODE] == "",
)


# ==========================================================================
# The projection: what the model sees, and what the record keeps
# (build ticket 06 - *How a conversation degrades as context grows*)
# ==========================================================================
print("\n-- the projection --")


def long_history(turns, size=4_000):
    """A stored conversation large enough to need trimming, in wire format."""
    history = []
    for index in range(turns):
        call_id = f"g{index}"
        history.append({"role": "user", "content": f"turn {index} asks for something"})
        history.append(
            {
                "role": "assistant",
                "content": f"working on {index}",
                "tool_calls": [wire_call(call_id, f"purpose {index}", "pass")],
            }
        )
        history.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"ok": True, "stdout": "y" * size}),
            }
        )
        history.append({"role": "assistant", "content": f"turn {index} is done"})
    return history


def stored_bytes(messages):
    return sum(context.message_bytes(message) for message in messages)


def last_turn_start(history):
    return max(
        index for index, message in enumerate(history) if message.get("role") == "user"
    )


def notes_in(session):
    return [message for message in session.messages if message.kind == conversation.KIND_NOTE]


long = conversation.Conversation()
long_sender = FakeSender()
long.attach(
    send=long_sender,
    execute=ok_executor,
    context=lambda: ("BASE", "LIVE SUMMARY"),
    # The addon's lever, set here the way `attach` injects it in a real session -
    # a budget small enough that a six-turn conversation has to give something up.
    budget=lambda: 900,
)
seed = long_history(6)
long.history[:] = [dict(message) for message in seed]
long.messages[:] = conversation.messages_from_history(long.history)
check("a stored conversation is not touched until a request is built", long.history == seed)
check("the chip is derived, so it is already on for a record this size", long.trimmed is True)

request = long.build_messages("BASE", "LIVE SUMMARY", "make it taller")
projection = request[1:-2]
check(
    "the base prompt is still index 0",
    request[0] == {"role": "system", "content": "BASE"},
)
check(
    "the live summary is still the last message",
    request[-1] == {"role": "system", "content": "LIVE SUMMARY"},
)
check(
    "the head marker goes straight after the base prompt",
    request[1]["role"] == "system"
    and "elided to fit the context window" in request[1]["content"],
)
check(
    "the projection is a sound subsequence of the store",
    context.validate_projection(long.history, projection, protected_from=None) == [],
)
check(
    "the oldest turns are the ones that went, and the newest settled one stayed",
    not any(
        message.get("content") == "turn 0 asks for something" for message in projection
    )
    and any(
        message.get("content") == "turn 4 asks for something" for message in projection
    ),
)
check(
    "the request is smaller than the record it was made from",
    stored_bytes(request) < stored_bytes(long.history),
)
check(
    "and the record itself is untouched by building it", long.history == seed
)

# A budget where elision *alone* is enough: then every turn stays and only the
# tool output goes, which is the "cheapest thing to lose" half of the order.
elide_only = conversation.Conversation()
elide_only.attach(
    send=FakeSender(),
    execute=ok_executor,
    context=lambda: ("BASE", "LIVE SUMMARY"),
    budget=lambda: 3_000,
)
elide_only.history[:] = [dict(message) for message in seed]
elide_only.messages[:] = conversation.messages_from_history(elide_only.history)
elide_only_request = elide_only.build_messages("BASE", "LIVE SUMMARY", "make it taller")
elide_only_projection = elide_only_request[1:-2]
check(
    "with room for the turns, only the tool output goes",
    any(
        message.get("role") == "tool" and '"elided": true' in (message.get("content") or "")
        for message in elide_only_projection
    )
    and all(
        f"turn {index} asks for something" in json.dumps(elide_only_projection)
        for index in range(6)
    ),
)
check(
    "and the elided stub still answers its call, so nothing dangles",
    context.validate_projection(elide_only.history, elide_only_projection) == [],
)
check(
    "the elision is announced rather than silent, and names no dropped turn",
    "tool result" in notes_in(elide_only)[0].text
    and "turn" not in notes_in(elide_only)[0].text,
)
check(
    "and its expander offers details rather than turns nobody dropped",
    notes_in(elide_only)[0].purpose == "details",
)

notes = notes_in(long)
check("a turn that had to trim leaves exactly one note", len(notes) == 1)
check(
    "the note says what the model did not see, in one sentence",
    notes[0].text.startswith("\u2702 context trimmed")
    and "model saw" in notes[0].text
    and notes[0].text.endswith("your history is kept"),
)
check(
    "expanding it names the turns that went",
    "turn 1:" in notes[0].detail
    and "messages" in notes[0].detail
    and "\u201c" in notes[0].detail,
)
check("and says why the order is the order", context.EVICTION_ORDER in notes[0].detail)
check(
    "its expander promises the turns that went, because turns went",
    notes[0].purpose == "which turns",
)
check(
    "it is the last row when the request is built, so it heads the turn being built",
    long.messages[-1].kind == conversation.KIND_NOTE,
)
check(
    "the trim is recorded for the file, beside the retention cap",
    long.trim_meta["last_trim"]["turns"] >= 1
    and long.trim_meta["turns_elided_total"] >= 1
    and bool(long.trim_meta["first_trim_at"]),
)
check(
    "and the note claims only loss that reached the wire: a stub step 2 then "
    "dropped is reported as dropped",
    "elided" not in notes[0].text and "dropped" in notes[0].text,
)

long.begin_turn("make it taller")
check("the turn in flight starts where the history ends", long.turn_start == len(seed))
deliver(long, "Looking first.", [wire_call("n1", "Inspect the scene", "pass")])
check(
    "a later round that trims again does not add a second note",
    len(notes_in(long)) == 1,
)
check("a tick runs the queued call", long.pump() is True)
check("the row carries its result", long._rows["n1"].status == conversation.STATUS_OK)
check("the next tick asks for the round after it", long.pump() is True)
check("exactly one request went out", len(long_sender.sent) == 1)

second = long_sender.sent[0]
tail = long.history[long.turn_start :]
check(
    "the round after a tool result is projected too",
    second[1]["role"] == "system" and "elided to fit the context window" in second[1]["content"],
)
check(
    "with the base prompt at index 0 and the summary last",
    second[0] == {"role": "system", "content": "BASE"}
    and second[-1] == {"role": "system", "content": "LIVE SUMMARY"},
)
check(
    "and the turn in flight is byte-identical inside it (I4)",
    second[:-1][-len(tail) :] == tail,
)
check(
    "that projection is sound too",
    context.validate_projection(long.history, second[1:-1], protected_from=long.turn_start)
    == [],
)

deliver(long, "Done: the cube is taller on Z.")
check("the long turn still gets answered", not long.streaming and long.messages[-1].text.startswith("Done"))
check("and its history is still sendable", unanswered(long.history) == [])
check(
    "the record keeps everything the model no longer saw",
    long.history[: len(seed)] == seed,
)
check(
    "so the request the model got stayed smaller than the record",
    stored_bytes(second) < stored_bytes(long.history),
)
check(
    "and the record grew by exactly the live turn, with nothing trimmed out of it",
    len(long.history) == len(seed) + 4
    and long.history[len(seed)] == {"role": "user", "content": "make it taller"},
)
# One note per *turn*, not per round and not per session. The first request of a
# turn is built before the turn opens, so a latch reset in `begin_turn` wipes the
# note the first request just wrote and the turn notes twice - which is exactly
# what the GUI probe caught and this file did not, until this check was added.
check(
    "the whole turn carries exactly one note, however many rounds trimmed",
    len(notes_in(long)) == 1,
)
long.begin_turn("now make it red")
long.build_messages("BASE", "LIVE SUMMARY")
check(
    "and the next turn that trims says so again, once",
    len(notes_in(long)) == 2,
)
long.cancel()

short_session = conversation.Conversation()
wire(short_session, FakeSender())
short_session.begin_turn("hello")
plain_request = short_session.build_messages("BASE", "LIVE SUMMARY")
check(
    "a conversation inside its budget is sent byte-identical to the store",
    plain_request
    == [{"role": "system", "content": "BASE"}]
    + short_session.history
    + [{"role": "system", "content": "LIVE SUMMARY"}],
)
check("with no chip and no note", not short_session.trimmed and notes_in(short_session) == [])
short_session.cancel()

long.clear()
check("the chip goes when the record does", long.trimmed is False)


# ==========================================================================
# The working indicator and the reading order.
#
# Both are answers to "the panel is a column that cannot scroll itself": the
# indicator says something is happening without a scroll, and newest-first puts
# the live turn under the controls. `tools/working_probe.py` photographs the
# motion; these checks cover the state behind it, which is where the phrases and
# the clock are decided.
# ==========================================================================
print("\n-- the working indicator --")

strip = conversation.Conversation()
check("an idle conversation has no turn clock", strip.elapsed() == 0.0)
check("and nothing to say about what it is doing", strip.busy_note() == "")

strip.begin_turn("make it taller")
check("a turn starts its clock", strip.turn_started is not None)
check("waiting is what a silent request looks like", strip.busy_note() == "Waiting\u2026")
check(
    "the clock reads the moment it is given, not the wall clock",
    strip.elapsed(strip.turn_started + 12.5) == 12.5,
)
check("and never a negative", strip.elapsed(strip.turn_started - 5.0) == 0.0)

strip.apply_event({"ev": "reasoning", "chars": 80})
check("a long think is named as thinking", strip.busy_note() == "Thinking\u2026")

strip.apply_event({"ev": "delta", "text": "Hel"})
check("text arriving is named as receiving", strip.busy_note() == "Receiving\u2026")

# The bound is measured: at the default sidebar width, the 22-character
# "Waiting for the model…" was photographed middle-clipped ("Waiting for the ...")
# beside the arc and the clock, because Blender clips rather than wraps a label.
NOTE_MAX = 14
notes = {"Running code\u2026", "Receiving\u2026", "Thinking\u2026", "Waiting\u2026"}
check("every phrase fits the width it was measured against", max(map(len, notes)) <= NOTE_MAX)
check("and they are four distinct states", len(notes) == 4)

strip.apply_event({"ev": "done", "finish_reason": "stop"})
check("the phrase goes with the turn", strip.busy_note() == "")
check("and so does the clock", strip.elapsed() == 0.0 and strip.turn_started is None)

queued = conversation.Conversation()
wire(queued, FakeSender())
queued.begin_turn("do it")
deliver(queued, calls=[wire_call("w1", "Do it", "pass")])
check("a queued call is named as running code", queued.busy_note() == "Running code\u2026")
check("even before its tick runs it", queued.running_tool is not None or bool(queued.pending))
queued.cancel()

# The sweeps are driven by the clock and never by events: during a model's think
# there are no events at all, so a counter would freeze the indicator in exactly
# the state it exists to announce.
check("the arc starts at the start of its cycle", conversation.ring_sweep(0.0) == 0.0)
check("and has gone round by the end of it", conversation.ring_sweep(conversation.RING_SECONDS) == 0.0)
check(
    "sweeping monotonically in between",
    conversation.ring_sweep(0.1) < conversation.ring_sweep(0.5) < conversation.ring_sweep(1.0),
)
check(
    "the bar goes there and back rather than nearly finishing",
    conversation.bar_sweep(0.0) == 0.0
    and conversation.bar_sweep(conversation.BAR_SECONDS / 2.0) == 1.0
    and conversation.bar_sweep(conversation.BAR_SECONDS) == 0.0,
)
check(
    "both stay inside the range a progress factor accepts",
    all(
        0.0 <= swathe(step / 40.0) <= 1.0
        for swathe in (conversation.ring_sweep, conversation.bar_sweep)
        for step in range(80)
    ),
)

# ---------------------------------------------------------------- ordering
print("\n-- the reading order --")

ordered = conversation.Conversation()
ordered.messages.extend(
    [
        conversation.Message("user", "first ask"),
        conversation.Message("assistant", "first reply"),
        conversation.Message("user", "second ask"),
        conversation.Message("assistant", kind=conversation.KIND_CODE, purpose="Do it", detail="x = 1"),
        conversation.Message("assistant", "second reply"),
    ]
)
check(
    "turns group a reply with the prompt that asked for it",
    [len(turn) for turn in conversation.turns(ordered.messages)] == [2, 3],
)
chronological = ordered.visible_lines()
newest_first = ordered.visible_lines(newest_first=True)
check(
    "chronological order puts the oldest turn first",
    next(i for i, l in enumerate(chronological) if "first reply" in l)
    < next(i for i, l in enumerate(chronological) if "second reply" in l),
)
check(
    "newest-first puts the newest turn first",
    next(i for i, l in enumerate(newest_first) if "second reply" in l)
    < next(i for i, l in enumerate(newest_first) if "first reply" in l),
)
check(
    "and neither order reverses the lines inside an exchange",
    next(i for i, l in enumerate(newest_first) if "second ask" in l)
    < next(i for i, l in enumerate(newest_first) if "code \u00b7" in l)
    < next(i for i, l in enumerate(newest_first) if "second reply" in l),
)
check(
    "so a prompt is never drawn below the answer it produced",
    next(i for i, l in enumerate(newest_first) if "second ask" in l)
    < next(i for i, l in enumerate(newest_first) if "second reply" in l),
)
check("both orders draw the same lines", sorted(chronological) == sorted(newest_first))

# The truncation keeps the newest lines in either order, because a panel that
# dropped the newest turn to save room would be hiding the answer.
bulk = conversation.Conversation()
for index in range(60):
    bulk.messages.append(conversation.Message("user", f"ask {index}"))
    bulk.messages.append(conversation.Message("assistant", f"reply {index}"))
kept_chrono = bulk.visible_lines()
kept_newest = bulk.visible_lines(newest_first=True)
check("a long transcript is truncated", len(kept_chrono) <= conversation.VISIBLE_LINES + 1)
check(
    "chronological order drops the note at the top and keeps the newest",
    kept_chrono[0].startswith("...") and "reply 59" in " ".join(kept_chrono),
)
check(
    "newest-first keeps the newest too, and says so at the far end",
    kept_newest[-1].startswith("...") and "reply 59" in " ".join(kept_newest),
)

# ==========================================================================
# Which credential a request carries (decision 05, wired 2026-09-26).
#
# `transport.config()` is the only function that decides, and it layers
# **preference -> environment -> default**. Two properties matter more than the
# individual layers, and both are checked here rather than assumed:
#
#   * the env-only call is unchanged, because every probe that predates the
#     preferences calls it with no argument - and the base URL's default must NOT
#     apply there, or a probe holding a faked key would be pointed at a real host
#     by nothing more than a missing variable;
#   * "neither" produces a sentence naming *both* routes, since a bare "not
#     configured" leaves the user unable to tell a missing setting from a wrong
#     screen. Ticket 05 §3.
# ==========================================================================
print("\n-- the transport config layering --")

_transport_spec = importlib.util.spec_from_file_location(
    "transport", _HERE.parent / "blender_copilot" / "transport.py"
)
assert _transport_spec and _transport_spec.loader
transport = importlib.util.module_from_spec(_transport_spec)
_transport_spec.loader.exec_module(transport)


class FakePrefs:
    """`BlenderCopilotPreferences` minus RNA: the same three field names.

    `_pref` reads them with `getattr(..., "")`, so this is the whole interface
    `config` needs from an add-on's preferences.
    """

    def __init__(self, api_key="", base_url="", model=""):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model


KEY_VAR, URL_VAR, MODEL_VAR = transport.KEY_ENV, transport.URL_ENV, transport.MODEL_ENV

# What the suite was started with, put back at the end. In-process only - a child
# cannot change its parent's environment - but this file is also the place a
# future check would read these variables, and leaving them blanked is exactly how
# a test becomes order-dependent.
_ORIGINAL_ENV = {name: os.environ.get(name) for name in (KEY_VAR, URL_VAR, MODEL_VAR)}


def credentials(api_key=None, url=None, model=None):
    """Set exactly these three variables - `None` means unset, never empty."""
    for name, value in ((KEY_VAR, api_key), (URL_VAR, url), (MODEL_VAR, model)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def restore_credentials():
    for name, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# -- the preference wins, all three of them -------------------------------
credentials(api_key="env-key", url="https://env.example", model="env-model")
preference = FakePrefs(api_key="pref-key", base_url="https://pref.example", model="pref-model")
chosen = transport.config(preference)
check("a preference's key beats the environment", chosen.api_key == "pref-key")
check("a preference's URL beats the environment", chosen.base_url == "https://pref.example")
check("a preference's model beats the environment", chosen.model == "pref-model")
check(
    "and the config says which route each came from",
    (chosen.key_source, chosen.url_source, chosen.model_source)
    == (transport.FROM_PREFS, transport.FROM_PREFS, transport.FROM_PREFS),
)
check("a set preference is no problem at all", chosen.problem == "")

# -- the environment fills in whatever the preference left empty ----------
half = transport.config(FakePrefs(api_key="pref-key"))
check("an empty URL field falls back to the environment", half.base_url == "https://env.example")
check("and so does an empty model field", half.model == "env-model")
check(
    "with the source reported as the environment, not as the field",
    (half.url_source, half.model_source) == (transport.FROM_ENV, transport.FROM_ENV),
)

# -- the default is last, and only for the in-Blender route ---------------
bare = transport.config(FakePrefs())
credentials()
empty_prefs = transport.config(FakePrefs())
check(
    "the base URL's default applies when a preferences object is given",
    empty_prefs.base_url == transport.DEFAULT_BASE_URL,
)
check("and it is the last layer, so nothing is overridden by it",
    empty_prefs.url_source == transport.FROM_DEFAULT)
check(
    "the model's default is the measured one, not the retired name",
    empty_prefs.model == transport.DEFAULT_MODEL == "deepseek-flash",
)
check("a default is not a configured credential", bool(empty_prefs.problem))

# -- the message names both routes ---------------------------------------
credentials()
unconfigured = transport.config(FakePrefs())
check(
    "the missing-key message names the preferences route",
    "Add-ons \u25b8 Copilot" in unconfigured.problem,
)
check(
    "and the shell route, with the exact incantation",
    "set -a; . ./.env; set +a" in unconfigured.problem,
)
check(
    "and the variable whose absence caused it",
    transport.KEY_ENV in unconfigured.problem,
)
check(
    "the key itself is never in the message",
    "sk-" not in unconfigured.problem,
)

# -- the env-only call still behaves exactly as it shipped ---------------
credentials(api_key="env-key", url="https://env.example", model="env-model")
env_only = transport.config()
check("env-only still reads the environment", env_only.api_key == "env-key")
check("including the URL", env_only.base_url == "https://env.example")
check(
    "and it reports the environment as the source",
    env_only.key_source == transport.FROM_ENV and env_only.problem == "",
)

credentials()
nothing = transport.config()
check(
    "env-only with nothing set does NOT invent a base URL",
    nothing.base_url == "" and bool(nothing.problem),
)
check(
    "so a probe with a faked key can never be sent to a real host by omission",
    transport.DEFAULT_BASE_URL not in (nothing.base_url or ""),
)
check("and that message names both routes too", "set -a; . ./.env; set +a" in nothing.problem)

# -- the fingerprint, which is what a human is shown ----------------------
check("a fingerprint is the last four characters", transport.fingerprint("sk-abc123XYZ") == "\u2026" + "3XYZ")
check("an absent key fingerprints as unset", transport.fingerprint("") == "not set")
check(
    "a key too short to fingerprint is not revealed",
    "abc" not in transport.fingerprint("abc"),
)
described = transport.config(FakePrefs(api_key="sk-secret-ABCD1234")).describe()
check("describe names all three sources", described.startswith("key: prefs "))
check("and carries no more than four characters of the key", "ABCD1234" not in described and "1234" in described)
check("while the never-secret URL and model are shown", "http" in described and "deepseek-flash" in described)
check("a repr of the config prints a length, never the key", "secret" not in repr(empty_prefs))

# -- normalisation -------------------------------------------------------
credentials(api_key="k", url="https://env.example/")
check("a trailing slash is stripped so the path cannot double", transport.config().base_url == "https://env.example")

credentials()
check(
    "an empty preference field is not a set field",
    transport.config(FakePrefs(api_key="   ")).key_source == transport.FROM_NONE,
)

# -- the per-request output ceiling --------------------------------------
# Follows VS Code's Copilot extension: the limit is a property of the *model* and
# every request asks for that model's maximum (`chatMLFetcher.ts`:
# `max_tokens: chatEndpoint.maxOutputTokens`), with a declared fallback for a model
# that has none (`openRouterProvider.ts`). It is checked here because the failure
# it replaces was invisible in every other test: a 2,048-token cap silently spent
# on thinking ended a live turn with an empty transcript.
credentials(api_key="k", url="https://env.example")
check(
    "a known model resolves to its own ceiling",
    transport.max_output_tokens("deepseek-flash") == transport.MAX_OUTPUT_TOKENS["deepseek-flash"],
)
check(
    "both documented models carry a ceiling",
    set(transport.MAX_OUTPUT_TOKENS) == {"deepseek-flash", "deepseek-v4-pro"},
)
check(
    "an unknown model falls back to the family's documented maximum",
    transport.max_output_tokens("some-gateway-alias") == transport.DEFAULT_MAX_OUTPUT_TOKENS,
)
check(
    "a config carries that ceiling, so the worker asks for it per request",
    transport.config(FakePrefs(model="deepseek-flash")).max_tokens == 384_000,
)
check(
    "and a hand-built config does too, which is what the wire probe uses",
    transport.Config("http://127.0.0.1:1", "k", "deepseek-flash", "").max_tokens == 384_000,
)
check(
    "the ceiling is not a cap that can truncate a turn's thinking",
    transport.DEFAULT_MAX_OUTPUT_TOKENS >= 100_000,
)

# The human's path, exactly: the key typed into the preferences page and nothing
# in the environment. A Blender launched from the Finder has no `.env`, so this is
# the configuration the acceptance test is actually run under - and it has to be
# complete, with no problem message, because the alternative is a first run that
# asks the user to also know an endpoint URL.
credentials()
field_only = transport.config(FakePrefs(api_key="sk-from-the-field"))
check("a key in the field alone is a complete configuration", field_only.problem == "")
check("with the documented endpoint filled in as the last layer", field_only.base_url == transport.DEFAULT_BASE_URL)
check("and the measured model as the last layer", field_only.model == "deepseek-flash")
check("and the field reported as where the key came from", field_only.key_source == transport.FROM_PREFS)
check(
    "and the environment did not have to be involved at all",
    (field_only.url_source, field_only.model_source) == (transport.FROM_DEFAULT, transport.FROM_DEFAULT),
)

restore_credentials()

print("\nall checks passed")
