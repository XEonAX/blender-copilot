#!/usr/bin/env python3
"""One tool call end to end, with a **fake provider on localhost** and no Blender.

    python3 tools/bounded_run.py 60 -- \\
        /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 \\
        tools/loop_wire_probe.py

Why this exists, and what it is *not*: `tools/transport_smoke.py` makes real,
billable requests to DeepSeek, and the ticket that spends the money is the one
reserved for it. This runs the same machinery - the real `_worker.py` child, the
real `transport.Worker`, the real loop, the real sandbox - against a scripted
HTTP server on 127.0.0.1. Nothing leaves the machine and nothing is billed.

What it therefore verifies, none of which any CPython check can:

  1. the request body really declares the tool (`tools[0].function.name`), on
     every round rather than only the first;
  2. `_worker.py` really reassembles **fragmented** `tool_calls` deltas into one
     call, with `function.arguments` as a **JSON string** (measured shape, ticket
     16 / AGENTS.md) - it is delivered split across three SSE deltas here;
  3. the loop really parses that string, runs the code, and puts the result in the
     *next* request as a `tool` message with the matching `tool_call_id`;
  4. a failing call's full traceback reaches the model on the following round;
  5. the turn ends and the transcript holds a code row and a result row.

Run under Blender's bundled interpreter: the worker child is `sys.executable`, so
the child needs the `requests` that only that interpreter has.

It never prints, echoes or logs the API key - the one here is a literal fake that
exists only to satisfy the worker's `Authorization` header.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "loop-wire-probe.txt"

FAKE_KEY = "sk-probe-not-a-real-key"
MODEL = "probe-model"
BASE_PROMPT = "You are a probe. Use the tool when asked to change something."
SUMMARY = "Live scene: 1 objects, mode OBJECT, active Cube (MESH), selection 1 (Cube)."

# The "model's" code. It comes back over the wire as a JSON string inside the
# tool call's arguments, exactly as the provider sends it.
CODE = (
    "obj = C.active_object\n"
    "obj.dimensions.z = 2.6\n"
    "print(f'{obj.name}: z -> {obj.dimensions.z}')\n"
)
FAILING_CODE = (
    "obj = C.active_object\n"
    "obj.dimensions.z = 9.9\n"
    "raise RuntimeError('native call blew up after the change')\n"
)

# `C` is the prelude's own name for bpy.context (ticket 06), so the fake here has
# to be shaped like a context for the loop's execution path to be real.
STATE = SimpleNamespace(
    name="Cube", dimensions=SimpleNamespace(z=2.0)
)
PRELUDE = {"bpy": SimpleNamespace(data=None), "C": SimpleNamespace(active_object=STATE)}

NOTES: list[str] = []
FAILURES: list[str] = []


def note(line: str) -> None:
    NOTES.append(line)
    print(f"WIRE | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


def load(name: str, path: Path):
    """Import a bpy-free module by path: the package itself imports `bpy`."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conversation = load("bc_conversation", ROOT / "blender_copilot" / "conversation.py")
execution = load("bc_execution", ROOT / "blender_copilot" / "execution.py")
transport = load("bc_transport", ROOT / "blender_copilot" / "transport.py")


# ---------------------------------------------------------------------------
# The scripted provider
# ---------------------------------------------------------------------------

def delta(payload: dict) -> dict:
    return {"choices": [{"index": 0, "delta": payload}]}


def finish(reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def tool_round(code: str, purpose: str, prose: str = "") -> list[dict]:
    """One round whose reply asks for `run_blender_python`, arguments fragmented.

    The fragmentation is the point: the provider streams `function.arguments` in
    pieces, so a worker that overwrites instead of concatenating loses the call.
    """
    arguments = json.dumps({"code": code, "purpose": purpose})
    cut_a, cut_b = 18, 41
    events = [delta({"role": "assistant"})]
    if prose:
        events.append(delta({"content": prose}))
    events.append(
        delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_wire_1",
                        "type": "function",
                        "function": {"name": "run_blender_python", "arguments": arguments[:cut_a]},
                    }
                ]
            }
        )
    )
    events.append(
        delta({"tool_calls": [{"index": 0, "function": {"arguments": arguments[cut_a:cut_b]}}]})
    )
    events.append(
        delta({"tool_calls": [{"index": 0, "function": {"arguments": arguments[cut_b:]}}]})
    )
    events.append(finish("tool_calls"))
    return events


def prose_round(text: str) -> list[dict]:
    return [delta({"role": "assistant"}), delta({"content": text}), finish("stop")]


ROUNDS = [
    tool_round(CODE, "Scale Cube 1.3x on Z", "The cube is active, so I will scale it."),
    prose_round("Done: the cube is taller on Z."),
    tool_round(FAILING_CODE, "Try a bigger scale", "Trying a larger change."),
    prose_round("That failed, so I stopped there."),
]


class Provider(BaseHTTPRequestHandler):
    """A scripted /chat/completions. Records every request body it was handed."""

    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparseable": True}
        # Headers are recorded by NAME only: one of them carries the key.
        Provider.requests.append(
            {
                "path": self.path,
                "header_names": sorted(self.headers.keys()),
                "authorized": bool(self.headers.get("Authorization")),
                "body": body,
            }
        )
        index = len(Provider.requests) - 1
        events = ROUNDS[index] if index < len(ROUNDS) else prose_round("(unscripted round)")
        payload = b"".join(
            f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events
        ) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: D102 - silence the stderr spam
        pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    note(f"scripted provider on 127.0.0.1:{port} - no request leaves this machine")

    config = transport.Config(
        base_url=f"http://127.0.0.1:{port}",
        api_key=FAKE_KEY,
        model=MODEL,
        problem="",
    )
    worker = transport.Worker()
    session = conversation.Conversation()
    session.attach(
        send=lambda messages: worker.send(config, messages, execution.SCHEMAS),
        execute=lambda call: execution.execute_tool(call, PRELUDE),
        context=lambda: (BASE_PROMPT, SUMMARY),
    )

    turns: list[str] = ["make the cube taller", "now make it much taller"]

    def drive(deadline: float) -> None:
        """What the drain timer does, tick for tick, minus the timer."""
        while time.monotonic() < deadline and (session.streaming or worker.busy):
            for event in worker.tick():
                session.apply_event(event)
            session.pump()
            time.sleep(0.02)
        for event in worker.tick():
            session.apply_event(event)

    for ordinal, prompt in enumerate(turns, start=1):
        text = prompt
        # Exactly panel.Send's order: build the request, put it on the wire, then
        # open the turn - so no event can arrive without a turn to land in.
        messages = session.wire_messages(text, BASE_PROMPT, SUMMARY)
        problem = worker.send(config, messages, execution.SCHEMAS)
        if problem:
            note(f"FAILED to start the worker: {problem}")
            break
        session.begin_turn(text)
        drive(time.monotonic() + 30)
        note(
            f"turn {ordinal}: phase={session.phase} streaming={session.streaming} "
            f"status={session.status!r} rows={len(session.messages)}"
        )

    worker.shutdown(1.0)
    server.shutdown()

    # -- 1. the tool was declared, every round ------------------------------
    requests = Provider.requests
    note(f"requests seen by the provider: {len(requests)}")
    check("four rounds went out in two turns", len(requests) == 4)
    check(
        "every round declares run_blender_python",
        all(
            (request["body"].get("tools") or [{}])[0]
            .get("function", {})
            .get("name")
            == execution.RUN_PYTHON
            for request in requests
        ),
    )
    check("the request was authorized", all(request["authorized"] for request in requests))

    # -- 2. the fragmented call came back as one call ------------------------
    check("the model's code ran and changed the object", STATE.dimensions.z == 9.9)
    codes = [message for message in session.messages if message.kind == conversation.KIND_CODE]
    rows = [message for message in session.messages if message.kind == conversation.KIND_TOOL]
    check("the transcript has a code row per call", len(codes) == 2)
    check("the code row names the call", codes[0].purpose == "Scale Cube 1.3x on Z")
    check("the code row carries the model's code", "dimensions.z = 2.6" in codes[0].detail)
    check("the transcript has a result row per call", len(rows) == 2)
    check("the first row is ok", rows[0].status == conversation.STATUS_OK)
    check("its output is behind the expander", '"ok": true' in rows[0].detail)
    check("the second row is an error", rows[1].status == conversation.STATUS_ERROR)
    check("its expander holds the traceback", "Traceback" in rows[1].detail)

    # -- 3. the round the model and call ids match --------------------------
    second = requests[1]["body"]["messages"]
    assistant = [message for message in second if message.get("role") == "assistant"][-1]
    results = [message for message in second if message.get("role") == "tool"]
    check("round two carried the assistant's tool_calls", bool(assistant.get("tool_calls")))
    check(
        "the arguments arrived as a JSON string",
        isinstance(assistant["tool_calls"][0]["function"]["arguments"], str),
    )
    check("round two carried a tool result", len(results) == 1)
    check(
        "the result answers the call's own id",
        results[0]["tool_call_id"] == assistant["tool_calls"][0]["id"],
    )
    check("and it is the sandbox's envelope", '"ok": true' in results[0]["content"])
    stdout = json.loads(results[0]["content"]).get("stdout", "")
    check("the code's print came back to the model", "Cube: z -> 2.6" in stdout)
    check(
        "the fragmented arguments survived whole",
        json.loads(assistant["tool_calls"][0]["function"]["arguments"])["code"] == CODE,
    )
    check("the base prompt is still index 0", second[0]["content"] == BASE_PROMPT)
    check("the live summary is still last", second[-1]["content"] == SUMMARY)

    # -- 4. the traceback reached the model on the following round ----------
    fourth = requests[3]["body"]["messages"]
    traceback_result = [message for message in fourth if message.get("role") == "tool"][-1]
    check(
        "the failing call's traceback is in the next request",
        "RuntimeError: native call blew up after the change" in traceback_result["content"],
    )
    check(
        "and the change it made before raising is reported, not undone",
        STATE.dimensions.z == 9.9,
    )

    # -- 5. the turn ended cleanly -----------------------------------------
    check("the turn ended", not session.streaming and session.phase == conversation.PHASE_IDLE)
    check("nothing is still queued", not session.pending)
    check("the last message is the model's closing prose", session.messages[-1].text.startswith("That failed"))

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
        note(f"wrote {LOG}")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        note(f"could not write {LOG}: {exc}")

    if FAILURES:
        for failure in FAILURES:
            note(f"FAILED: {failure}")
        print("SMOKE FAILED", flush=True)
        return 1
    print("SMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    # `main()` RETURNS its code rather than raising SystemExit, so the guard below
    # cannot mistake a clean exit for a crash - which it did on the first run of
    # this probe, printing SMOKE OK and SMOKE FAILED in the same breath.
    try:
        exit_code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("SMOKE FAILED", flush=True)
        exit_code = 1
    sys.exit(exit_code)
