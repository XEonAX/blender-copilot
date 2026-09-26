#!/usr/bin/env python3
"""How long model-authored code may run, measured on the installed Blender.

    python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/budget_probe.py

    # the one case whose subject IS a hang, in its own bounded child:
    BC_BUDGET_CASE=swallow_hang python3 tools/bounded_run.py 20 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/budget_probe.py

**The budget is real; the wire is faked.** Everything that decides whether a call
is stopped is shipped code - `budget.py`'s alarm, `execution.run_python`'s window,
the loop's verdict handling in `conversation.py`, `undo_blender` and
`bpy.ops.ed.undo_push` - and every case is driven through `stream._tick()`, which
is the function `bpy.app.timers` calls (timers never pump under `-b`, which is why
the probe drives them itself). The only fake is the transport: a scripted worker
stands in for `transport.Worker`, so no request leaves the machine and no
credential is read.

The constructs are ticket 17's, re-measured against *this* implementation rather
than quoted from the ticket that designed it. What each case is evidence for:

    1  a pure-Python infinite loop is interrupted, and the turn it belonged to
       still leaves one Ctrl+Z step behind it (the undo is measured, not assumed:
       the change is made, the loop runs on, the call is stopped, `ed.undo()` is
       called once, and the change is gone)                          boxes 1, 4
    2  `time.sleep` is interrupted, not waited out                   box 2's class
    3  a blocking `recv` is interrupted                             box 2's class
    4  a single long NumPy call is *not* interrupted - it is stopped only once it
       returns, and the verdict says so, which is the case the panel must not
       over-promise about                                          boxes 2, 3
    5  code that swallows the interrupt and returns is still a stop, and says so
    6  code that catches the interrupt twice is interrupted a third time, so
       catching it buys a tick and not the call
    7  code that switches the budget off is *noticed*, and its turn ends    box 6
    8  the turn's cumulative clock ends a call the call's own budget would allow
    9  CONTROL: a call inside its budget is not stopped at all, and no alarm is
       left armed on Blender's SIGALRM afterwards
   10  CONTROL + the hang: BC_BUDGET_CASE=swallow_hang runs code that catches the
       interrupt forever. Nothing stops it - ticket 17 measured that under every
       mechanism tried - and the observation is `BOUNDED | KILLED` from the driver
       rather than a verdict from inside. That is the case the panel's copy calls
       unstoppable.                                                   box 6

Numbers are shortened (0.3-1.0 s rather than the shipped 15 s / 60 s) so the whole
probe is bounded by seconds and not by minutes; the shipped pair is checked as
copy, and `tools/panel_draw_smoke.py` checks that panel, tool description and
prompt all name the same two figures.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import time
from pathlib import Path

import bpy

# A literal fake: the worker is replaced below, so nothing is sent and `.env` is
# never read.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
# The hang case gets its own file: it is a run whose whole result is "the child was
# killed", so it never reaches the verdict that writes the shared log.
LOG = ROOT / "logs" / (
    "budget-probe-hang.txt"
    if os.environ.get("BC_BUDGET_CASE") == "swallow_hang"
    else "budget-probe.txt"
)

# The probes' conversations must not land in the user's real extension directory.
os.environ.setdefault("BLENDER_COPILOT_HISTORY_DIR", "/tmp/blender-copilot-budget-probe")

NOTES: list[str] = []
FAILURES: list[str] = []

bc = None
worker = None
settings = None
session = None


def note(line: str) -> None:
    NOTES.append(line)
    print(f"BUDGET | {line}", flush=True)


def record(payload: dict) -> None:
    """One machine-readable line per case, the way `runaway_probe.py` reports."""
    note("BC|" + json.dumps(payload, sort_keys=True, default=str))


def flush_log() -> None:
    """Write what is known so far. Called by the case that will not come back."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"BUDGET | could not write {LOG}: {exc}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return condition


# ---------------------------------------------------------------------------
# The scripted transport: the worker protocol, no pipe and no HTTP.
# ---------------------------------------------------------------------------

class ScriptedWorker:
    def __init__(self) -> None:
        self.rounds: list[list[dict]] = []
        self.sent: list[dict] = []
        self._queue: list[dict] = []

    @property
    def busy(self) -> bool:
        return bool(self._queue or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.sent.append({"messages": messages, "tools": tools})
        self._queue.extend(self.rounds.pop(0) if self.rounds else [])
        return None

    def tick(self) -> list[dict]:
        events, self._queue = self._queue, []
        return events

    def cancel(self) -> None:
        self._queue = []
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        pass


def tool_round(call_id: str, purpose: str, code: str) -> list[dict]:
    return [
        {
            "ev": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_blender_python",
                        "arguments": json.dumps({"code": code, "purpose": purpose}),
                    },
                }
            ],
        }
    ]


def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


# ---------------------------------------------------------------------------
# Driving the shipped loop, the way its own timer does
# ---------------------------------------------------------------------------

def send(prompt: str, rounds: list[list[dict]]) -> str:
    worker.rounds = list(rounds)
    settings.prompt_text = prompt
    result = bpy.ops.blender_copilot.send()
    # `stream.start()` registered the real timer; under `-b` it never fires, and
    # leaving it registered would let a GUI run drive the same turn twice.
    bc.stream.stop()
    return "FINISHED" if "FINISHED" in result else ""


def drive(ticks: int = 200) -> bool:
    for _ in range(ticks):
        bc.stream._tick()
        if not session.streaming:
            return True
    return False


def arm(call_seconds: float, turn_seconds: float | None = None, poll: float = 0.2):
    """Shorten the shipped budget for one case, through the shipped object.

    The probe deliberately does not build a private `Limits`: the point is to
    measure the path the addon uses, which means `budget.LIMITS` - the very object
    `execution.run_python` opens a call window on and `conversation` opens the turn
    on. The two are one object by construction (asserted in
    `tools/panel_draw_smoke.py`), so this is one edit and not two.
    """
    limits = bc.budget.LIMITS
    limits.call_seconds = float(call_seconds)
    limits.turn_seconds = float(turn_seconds if turn_seconds is not None else 600.0)
    limits.poll = float(poll)
    return limits


def case(name: str, code: str, prompt: str, budget: float, turn_budget: float | None = None,
         poll: float = 0.2, closing: bool = False) -> dict:
    """One real turn whose only tool call is `code`. Returns what was observed.

    A turn, not a bare `run_python` call, on purpose: the ticket is about what
    happens to the *turn* when its code is stopped, and the turn is where the undo
    step, the transcript row and the next round live.
    """
    session.clear()
    limits = arm(budget, turn_budget, poll)
    rounds = [tool_round(f"{name}_1", prompt, code)]
    if closing:
        rounds.append(prose_round("Done."))
    sent_before = len(worker.sent)
    started = time.monotonic()
    ok = send(prompt, rounds)
    if not ok:
        FAILURES.append(f"Send refused for case {name}")
    drive()
    elapsed = time.monotonic() - started
    verdict = limits.verdict()
    envelope = None
    rows = [m for m in session.messages if m.kind == "tool"]
    if rows and rows[-1].detail:
        try:
            envelope = json.loads(rows[-1].detail)
        except ValueError:
            envelope = None
    payload = {
        "case": name,
        "elapsed": round(elapsed, 3),
        "verdict": verdict,
        "stopped": bool(verdict.get("interrupted") or verdict.get("interrupts") or verdict.get("disarmed")),
        "phase": session.phase,
        "streaming": bool(session.streaming),
        "row_status": rows[-1].status if rows else None,
        "error_kind": ((envelope or {}).get("error") or {}).get("kind"),
        "stdout": (envelope or {}).get("stdout") or "",
        "sentence": session.messages[-1].text if session.messages else "",
        "requests": len(worker.sent) - sent_before,
    }
    record(payload)
    return payload


def last_error_text() -> str:
    for message in reversed(session.messages):
        if message.kind == "error":
            return message.text
    return ""


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------

CODE_LOOP = """obj = C.active_object
obj.scale.z = 2.0
C.view_layer.update()
print(f"{obj.name}: z scale is now {obj.scale.z:.2f}")
while True:
    pass"""


def case_1() -> None:
    """A pure-Python infinite loop ends, and the turn still has a Ctrl+Z step."""
    session.clear()
    cube = bpy.context.active_object
    cube.scale.z = 1.0
    bpy.context.view_layer.update()
    note(f"cube scale.z before the loop: {scale_z()}")
    observed = case("loop", CODE_LOOP, "Spin forever", 1.0, 600.0, 0.25)
    check("the call was interrupted", observed["verdict"].get("interrupted") is True)
    check("on the call's own budget, not the turn's", observed["verdict"].get("kind") == "call")
    check("within a tick of the deadline it was given", observed["verdict"]["seconds"] < 1.6)
    check("the row is an error, not a running row", observed["row_status"] == "error")
    check(
        "the envelope names the budget rather than a traceback",
        observed["error_kind"] == "budget",
    )
    check(
        "and whatever the code printed before the loop came back with the result",
        "z scale is now 2.00" in observed["stdout"],
    )
    check("the turn ended rather than continuing", not observed["streaming"])
    check("the panel says the code ran past its budget", "ran past its budget" in last_error_text())
    check("no further request went out", observed["requests"] == 1)

    note(f"cube scale.z after the stopped turn: {scale_z()}")
    check("the half-applied change is still there, not unwound", scale_z() == 2.0)
    receipt = session.last_receipt
    note(f"receipt: {receipt}")
    check("the stopped turn left a receipt", bool(receipt))
    check("and the receipt says the step is undoable", bool(receipt and receipt["undoable"]))
    undone = bpy.ops.ed.undo()
    note(f"one undo of the stopped turn -> {undone}")
    check("one Ctrl+Z reverts the interrupted turn", scale_z() == 1.0)
    # Re-fetch: the undo replaced the datablock out from under the name, so holding
    # the old reference would raise `ReferenceError` on the next case.
    cube = bpy.context.active_object
    if cube is not None:
        cube.scale.z = 1.0
    bpy.context.view_layer.update()


def case_2() -> None:
    """`time.sleep`, which ticket 17 measured as stoppable at 1.00 s."""
    observed = case("sleep", "import time\nprint('sleeping')\ntime.sleep(30)", "Sleep for 30s", 1.0, 600.0, 0.25)
    check("a sleep is interrupted, not waited out", observed["verdict"].get("interrupted") is True)
    check("well before the sleep would have finished", observed["elapsed"] < 4.0)
    check("and it is not reported as a late raise", observed["verdict"].get("late") is False)


def case_3() -> None:
    """A blocking read with no peer, which ticket 17 measured as stoppable."""
    code = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', 0))\n"
        "s.listen(1)\n"
        "print('waiting for a peer that never comes')\n"
        "s.accept()"
    )
    observed = case("recv", code, "Block on a socket", 1.0, 600.0, 0.25)
    check("a blocking read is interrupted", observed["verdict"].get("interrupted") is True)
    check("before it could ever return", observed["elapsed"] < 4.0)


def case_4() -> None:
    """A single long native call: only stopped once it returns (ticket 17, 5.34 s
    for a 1 s budget on a 3000x3000 SVD). This is the case the panel's copy must
    not promise anything better for.

    The size is chosen from a measurement rather than guessed: the bundled NumPy
    2.3.4 on this machine takes 0.39 s for 1400^2, 1.12 s for 2000^2, 2.38 s for
    2400^2 and 4.69 s for 2800^2. 2400 is the smallest that overruns a 0.3 s budget
    by many times `LATE_SECONDS`, which is the property under test - a first
    attempt at 1400^2 returned in 0.44 s and was genuinely *not* late, so the check
    was measuring nothing.
    """
    code = (
        "import numpy as np\n"
        "a = np.random.rand(2400, 2400)\n"
        "print('starting svd')\n"
        "np.linalg.svd(a)"
    )
    observed = case("native", code, "Run a big SVD", 0.3, 600.0, 0.1)
    verdict = observed["verdict"]
    note(f"native call: elapsed={observed['elapsed']}s for a 0.3s budget -> {verdict}")
    check("the call was still interrupted in the end", verdict.get("interrupted") is True)
    check(
        "but late: the raise waited for the native call to return",
        verdict.get("late") is True and verdict["seconds"] > 0.3 + bc.budget.LATE_SECONDS,
    )
    check(
        "and the panel says a native call cannot be interrupted once started",
        "cannot be interrupted once it has started" in last_error_text(),
    )


def case_5() -> None:
    """Swallow the interrupt and return: the call is not 'ok' in the sense that
    matters, and the turn ends rather than buying the model another round."""
    code = (
        "import time\n"
        "try:\n"
        "    time.sleep(30)\n"
        "except BaseException:\n"
        "    print('swallowed the interrupt')\n"
    )
    observed = case("swallow", code, "Catch the interrupt", 0.3, 600.0, 0.5)
    verdict = observed["verdict"]
    note(f"swallow: {verdict}")
    check("the interrupt was delivered", verdict.get("interrupts") >= 1)
    check("and the code came back rather than unwinding", verdict.get("interrupted") is False)
    check(
        "so it is still a stop: the loop ends the turn",
        observed["streaming"] is False,
    )
    check("and the panel names what the code did", "caught the interrupt" in last_error_text())
    check("no further request went out", observed["requests"] == 1)


def case_6() -> None:
    """Catch it twice, and the alarm comes back a third time: the repeating timer
    is why a swallowed raise buys a tick and not the rest of the call."""
    code = (
        "import time\n"
        "for _ in range(2):\n"
        "    try:\n"
        "        time.sleep(30)\n"
        "    except BaseException:\n"
        "        pass\n"
        "time.sleep(30)\n"
    )
    observed = case("swallow_twice", code, "Catch it twice", 0.3, 600.0, 0.2)
    verdict = observed["verdict"]
    note(f"swallow twice: {verdict}")
    check("the raise was delivered more than once", verdict.get("interrupts") >= 3)
    check("and the last one took", verdict.get("interrupted") is True)
    check("so the call ended inside a second or so", observed["elapsed"] < 4.0)
    check(
        "it is not reported as a native call that returned late",
        verdict.get("late") is False
        and "cannot be interrupted" not in last_error_text(),
    )


def case_7() -> None:
    """`signal.setitimer(ITIMER_REAL, 0)`: ticket 17 measured this as HUNG under
    every mechanism. It cannot be prevented from inside the process, so it is
    *detected* and its turn is ended - which is what this measures."""
    code = (
        "import signal\n"
        "print('switching the budget off')\n"
        "signal.setitimer(signal.ITIMER_REAL, 0)\n"
    )
    observed = case("disarm", code, "Switch the budget off", 5.0, 600.0, 0.2)
    verdict = observed["verdict"]
    note(f"disarm: {verdict}")
    check("the code returned, so nothing was interrupted", verdict.get("interrupted") is False)
    check("but the timer was read back and found gone", verdict.get("disarmed") is True)
    check("so the turn ends", observed["streaming"] is False)
    check("with the panel saying the budget was switched off", "switched the budget off" in last_error_text())
    first, interval = signal.getitimer(signal.ITIMER_REAL)
    check("and the probe leaves no alarm armed on Blender", first == 0.0 and interval == 0.0)


def case_8() -> None:
    """The turn's cumulative clock. The call's own budget is ten seconds away, so
    only the turn's can end this one."""
    observed = case("turn", "import time\ntime.sleep(30)", "Sleep past the turn budget", 10.0, 0.5, 0.1)
    verdict = observed["verdict"]
    note(f"turn budget: {verdict}")
    check("the turn's clock ended the call", verdict.get("interrupted") is True)
    check("and it says which budget it was", verdict.get("kind") == "turn")
    check("against what was left of the turn", verdict.get("limit") <= 0.6)
    check("the panel names the turn's own figure", "call time" in last_error_text())


def case_9() -> None:
    """CONTROL: a call inside its budget is a call like any other."""
    observed = case(
        "clean", "print('fine')", "Just look", 5.0, 600.0, 0.2, closing=True
    )
    verdict = observed["verdict"]
    note(f"clean control: {verdict}")
    check("nothing was interrupted", verdict.get("interrupted") is False)
    check("no interrupt was delivered", verdict.get("interrupts") == 0)
    check("and nothing was disarmed", verdict.get("disarmed") is False)
    check("the call's row is ok", observed["row_status"] == "ok")
    check("the loop asked for the next round", observed["requests"] == 2)
    check("and the turn finished normally", not observed["streaming"])
    check("with no stop sentence in the transcript", last_error_text() == "")


def case_hang() -> None:
    """The unstoppable one, as a bounded child: code that catches the interrupt
    forever. Nothing in the addon can end this; the driver's deadline does."""
    note("BC|" + json.dumps({"case": "swallow_hang", "expect": "never returns"}))
    note("this child will be killed by the driver: that kill IS the observation")
    flush_log()
    code = (
        "import time\n"
        "print('catches the interrupt forever')\n"
        "while True:\n"
        "    try:\n"
        "        time.sleep(0.05)\n"
        "    except BaseException:\n"
        "        pass\n"
    )
    session.clear()
    arm(1.0, 600.0, 0.25)
    send("Catch it forever", [tool_round("hang_1", "Never stop", code)])
    # The next tick runs the call on the main thread and never comes back. The loop
    # below exists so that "it returned" would be recorded rather than assumed; if
    # the code were stoppable, this would finish and the run would report it.
    for _ in range(40):
        bc.stream._tick()
        time.sleep(0.25)
    note("the call returned on its own, which would contradict the premise")
    flush_log()


def scale_z() -> float | None:
    cube = bpy.data.objects.get("Cube")
    return None if cube is None else round(cube.scale.z, 4)


def finish(code: int = 0) -> None:
    flush_log()
    for failure in FAILURES:
        print(f"BUDGET | FAILED: {failure}", flush=True)
    print("BUDGET OK" if not FAILURES and code == 0 else "BUDGET FAILED", flush=True)
    if FAILURES or code:
        sys.exit(1)


def main() -> int:
    global bc, worker, settings, session

    bc = importlib.import_module(EXT)
    addon = bpy.context.preferences.addons.get(EXT)
    if addon is None:
        # `--factory-startup` does not load user preferences, so the extension is
        # installed but not enabled. Enabling it in-script is what makes this run
        # both reproducible and real: the extension's own `register`, its
        # handlers, its operators and its `stream` wiring.
        bpy.ops.preferences.addon_enable(module=EXT)
        addon = bpy.context.preferences.addons.get(EXT)
    check("the extension registers with preferences", addon is not None and addon.preferences is not None)
    if addon is None or addon.preferences is None:
        return 1
    settings = addon.preferences
    settings.layout_variant = "boxes"

    worker = ScriptedWorker()
    bc.transport.worker = worker
    session = bc.conversation.session

    # One ledger, checked rather than assumed: the loop's turn clock and the
    # sandbox's call window must be the same object, or "60 s per turn" is two
    # different 60 s figures and the failure is silent.
    check("the session's budget is the sandbox's budget", session.limits is bc.budget.LIMITS)
    check("and the sandbox's default is that same object", bc.execution.budget.LIMITS is bc.budget.LIMITS)
    check(
        "the shipped figures are still ticket 17's",
        (bc.budget.CALL_SECONDS, bc.budget.TURN_SECONDS) == (15.0, 60.0),
    )
    check("Global Undo is on, which case 1 assumes", bool(bpy.context.preferences.edit.use_global_undo))

    # A scene this probe can make claims about: one cube, active, at a known size.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablock in (bpy.data.objects, bpy.data.meshes):
        for item in list(datablock):
            if item.users == 0:
                datablock.remove(item)
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.view_layer.update()
    note(f"blender {bpy.app.version_string}, background={bpy.app.background}")
    note(f"scene prepared: {sorted(o.name for o in bpy.data.objects)}, active={bpy.context.active_object.name}")

    if os.environ.get("BC_BUDGET_CASE") == "swallow_hang":
        case_hang()
        finish()
        return 0

    case_1()
    case_2()
    case_3()
    case_4()
    case_5()
    case_6()
    case_7()
    case_8()
    case_9()

    # Leave Blender's timer as it was found, and prove it: a probe that walks away
    # with SIGALRM armed would change the very thing it is measuring for whoever
    # runs next in this process.
    limits = bc.budget.LIMITS
    limits.call_seconds = bc.budget.CALL_SECONDS
    limits.turn_seconds = bc.budget.TURN_SECONDS
    limits.poll = bc.budget.REPEAT_SECONDS
    first, interval = signal.getitimer(signal.ITIMER_REAL)
    check("no alarm is left armed at the end of the run", first == 0.0 and interval == 0.0)
    finish()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("BUDGET FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
