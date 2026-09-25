#!/usr/bin/env python3
"""One real turn, end to end, without a GUI.

    set -a; . ./.env; set +a
    python3 tools/bounded_run.py 180 -- \\
        /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 \\
        tools/transport_smoke.py

**Why this exists.** `bpy.app.timers` never pump under `blender -b`, so the
panel's repaint cannot be exercised headlessly at all - that part is the human's
eye. Everything *under* the repaint can be: this drives the real
`blender_copilot/transport.py` (the real worker subprocess, the real SSE stream,
the real accumulation) and feeds every event through
`conversation.Conversation.apply_event`, which is exactly what the drain timer
does with it. The one thing it does not prove is that Blender calls the timer.

It makes **two** real requests - a full turn, then a cancelled one - and prints a
verdict token, because a caller has to be able to gate on something that is not
an exit code. It reads the key from the environment and never prints it. The
transcript goes to `logs/transport-smoke.txt` so the evidence survives the
terminal.

Blender's bundled interpreter is used deliberately: it is the interpreter the
addon launches as its child, and the one whose `requests`/`certifi` do the work.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "transport-smoke.txt"
POLL = 0.02
DEADLINE = 120.0

NOTES: list[str] = []


def load(name: str, path: Path):
    """Import a module by path.

    `blender_copilot/__init__.py` imports `bpy`, which does not exist in the
    bundled interpreter, so the package cannot be imported here - and both
    modules this needs are bpy-free by design.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conversation = load("bc_conversation", ROOT / "blender_copilot" / "conversation.py")
transport = load("bc_transport", ROOT / "blender_copilot" / "transport.py")

BASE_PROMPT = (
    "You are a test harness for a Blender addon's network transport. Answer in "
    "one short sentence. Do not use headings or lists."
)
SUMMARY = "Live scene: unavailable. This is a harness, not Blender."


def note(line: str) -> None:
    NOTES.append(line)
    print(f"SMOKE | {line}", flush=True)


def pump(session, until, deadline: float) -> list[dict]:
    """Exactly what the drain timer does, minus the timer."""
    events: list[dict] = []
    while time.monotonic() < deadline:
        for event in transport.worker.tick():
            events.append(event)
            session.apply_event(event)
        if until(events):
            return events
        time.sleep(POLL)
    return events


def write_log() -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"SMOKE | could not write {LOG}: {exc}", flush=True)


def main() -> int:
    cfg = transport.config()
    if cfg.problem:
        note(f"FAILED before any request: {cfg.problem}")
        write_log()
        print("SMOKE FAILED", flush=True)
        return 1
    note(f"endpoint {cfg.base_url}, model {cfg.model}, key present (never printed)")

    session = conversation.Conversation()
    failures: list[str] = []

    # -- 1. a full turn ------------------------------------------------------
    prompt = "Reply with the single word: pong"
    messages = session.wire_messages(prompt, BASE_PROMPT, SUMMARY)
    started = time.monotonic()
    problem = transport.worker.send(cfg, messages)
    if problem:
        note(f"FAILED to start the worker: {problem}")
        write_log()
        print("SMOKE FAILED", flush=True)
        return 1
    session.begin_turn(prompt)
    note(f"turn 1 sent after {time.monotonic() - started:.3f}s (no wait for ready)")

    first_delta = None
    deadline = time.monotonic() + DEADLINE

    def finished(events: list[dict]) -> bool:
        nonlocal first_delta
        for event in events:
            if event.get("ev") == "delta" and first_delta is None:
                first_delta = time.monotonic() - started
            if event.get("ev") in ("done", "error", "startup_failed"):
                return True
        return False

    events = pump(session, finished, deadline)
    kinds = [event.get("ev") for event in events]
    reasoning = sum(event.get("chars", 0) for event in events if event.get("ev") == "reasoning")
    seen = ", ".join(sorted(set(kinds)))
    note(f"turn 1 events: {len(events)} ({seen})")
    note(
        "turn 1 first delta after "
        + (f"{first_delta:.3f}s" if first_delta is not None else "(never)")
    )
    note(f"turn 1 reasoning characters seen: {reasoning}")

    if "done" not in kinds:
        failures.append(f"turn 1 never finished: {kinds[-3:]}")
    reply = session.messages[-1].text if session.messages else ""
    if session.messages and session.messages[-1].kind == conversation.KIND_ERROR:
        failures.append(f"turn 1 ended in an error block: {session.messages[-1].text}")
    if not reply.strip():
        failures.append("turn 1 produced no visible text")
    if session.streaming:
        failures.append("turn 1 left the session streaming")
    note(f"turn 1 reply: {reply.strip()[:200]!r}")

    # -- 2. a cancelled turn, on the same child ------------------------------
    # Long enough that the cancel certainly lands mid-stream: an immediate
    # "pong" would finish before Stop could be pressed and prove nothing.
    prompt2 = "Write an essay of at least 400 words about the history of Blender."
    messages2 = session.wire_messages(prompt2, BASE_PROMPT, SUMMARY)
    problem = transport.worker.send(cfg, messages2)
    if problem:
        failures.append(f"turn 2 could not be sent: {problem}")
    else:
        session.begin_turn(prompt2)
        cancel_at = None

        def first_text(events: list[dict]) -> bool:
            return any(event.get("ev") == "delta" for event in events)

        events2 = pump(session, first_text, time.monotonic() + DEADLINE)
        if not any(event.get("ev") == "delta" for event in events2):
            failures.append("turn 2 produced no delta to cancel during")
        else:
            cancel_at = time.monotonic()
            session.cancel()          # the UI acts now
            transport.worker.cancel()  # the child is told
            events3 = pump(
                session,
                lambda seen: any(event.get("ev") == "stopped" for event in seen),
                time.monotonic() + transport.CANCEL_GRACE + 5.0,
            )
            took = time.monotonic() - cancel_at
            stopped = any(event.get("ev") == "stopped" for event in events3)
            note(
                f"turn 2 stopped after {took:.2f}s "
                f"({'the child acknowledged' if took < transport.CANCEL_GRACE else 'the grace-kill fired'})"
            )
            if not stopped:
                failures.append("cancel never produced a stopped event")
            if session.streaming:
                failures.append("cancel left the session streaming")
            if session.messages[-1].text and not session.messages[-1].text.endswith("[stopped]"):
                failures.append("the cancelled reply is not marked stopped")
            note(f"turn 2 kept {len(session.messages[-1].text)} characters, marked [stopped]")

    if not transport.worker.alive:
        failures.append("the worker died; one child should serve both turns")

    transport.worker.shutdown()

    transcript = session.transcript_text()
    note("transcript after both turns:")
    NOTES.append(transcript)
    if cfg.api_key in transcript or cfg.api_key in "\n".join(NOTES):
        failures.append("THE API KEY REACHED THE LOG")
    write_log()

    if failures:
        for failure in failures:
            note(f"FAILED: {failure}")
        print("SMOKE FAILED", flush=True)
        return 1
    print("SMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
