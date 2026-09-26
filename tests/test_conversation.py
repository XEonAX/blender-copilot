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
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
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

unknown = execution.execute_tool(
    {"id": "a2", "function": {"name": "get_scene_info", "arguments": "{}"}}, {}
)
check(
    "an unknown tool is named and runs nothing",
    unknown["ok"] is False
    and unknown["envelope"]["error"]["kind"] == "tool_argument_error"
    and "get_scene_info" in unknown["envelope"]["error"]["message"],
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


def wire(session, sender, execute=ok_executor):
    session.attach(send=sender, execute=execute, context=lambda: ("BASE", "LIVE SUMMARY"))
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

print("\nall checks passed")
