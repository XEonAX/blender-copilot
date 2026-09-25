"""Runaway / hang protection: measure what a `sys.monitoring` budget can stop.

Ticket: `.scratch/blender-copilot/issues/17-runaway-and-hang-protection.md`

Run it as:

    /Applications/Blender.app/Contents/MacOS/Blender --factory-startup -b \
        -P tools/runaway_probe.py

The script is both driver and child.  With `BC_RUNAWAY_CASE` set it runs one
case and prints a `BC|{json}` line; without it, it re-invokes Blender once per
case (so an unkillable hang is a killed child, not a killed session) and prints
the table.  Cases that cannot be decided headlessly are named as such.

Why a child process per case: the question is *"can this be stopped at all"*.
The only honest way to answer for a construct that cannot be stopped is to run
it somewhere the parent can shoot.  `bpy.app.binary_path` re-launches the
installed 5.2.2; `--factory-startup` keeps the user config untouched.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

MODULE = "<copilot>"  # synthetic co_filename of every exec'd model turn
CHILD_ENV = "BC_RUNAWAY_CASE"
TOOL_ID = sys.monitoring.PROFILER_ID  # 2; DEBUGGER/COVERAGE/OPTIMIZER are 0/1/5


class BudgetExceeded(BaseException):
    """Deliberately BaseException: model `except Exception` must not eat it."""


# ---------------------------------------------------------------------------
# Budget implementations (reference, for measurement)
# ---------------------------------------------------------------------------


class _FilteredBudget:
    """Global LINE/INSTRUCTION events, only the model's synthetic file charged.

    `co_filename` filtering is what keeps the *runner's* own lines out of the
    budget.  Without it the callback fires on the caller's `try:` line before
    `SETUP_FINALLY` runs, and the interrupt escapes the very `try` meant to
    catch it (measured; see the `naive_*` cases).
    """

    event = sys.monitoring.events.LINE
    label = "line"

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0
        self.over = False
        self.max_depth = 0
        self._depth = 0

    def _on_event(self, code, arg):
        if code.co_filename != MODULE:
            return
        self._depth += 1
        if self._depth > self.max_depth:
            self.max_depth = self._depth
        try:
            self.count += 1
            if self.count > self.limit and not self.over:
                self.over = True
                # Disarm before raising: the unwind itself runs Python lines in
                # the runner, and a still-armed budget would re-raise there.
                sys.monitoring.set_events(TOOL_ID, 0)
                raise BudgetExceeded(f"{self.label} budget {self.limit} exceeded")
        finally:
            self._depth -= 1

    def install(self):
        sys.monitoring.use_tool_id(TOOL_ID, "blender_copilot_budget")
        sys.monitoring.register_callback(TOOL_ID, self.event, self._on_event)
        sys.monitoring.set_events(TOOL_ID, self.event)

    def disarm(self):
        sys.monitoring.set_events(TOOL_ID, 0)
        try:
            sys.monitoring.register_callback(TOOL_ID, self.event, None)
            sys.monitoring.free_tool_id(TOOL_ID)
        except Exception:
            pass


class FilteredLineBudget(_FilteredBudget):
    event = sys.monitoring.events.LINE
    label = "line"


class FilteredInstructionBudget(_FilteredBudget):
    event = sys.monitoring.events.INSTRUCTION
    label = "instruction"


class NaiveLineBudget:
    """No filename filter, no latch: every monitored line after the limit raises.

    This models the obvious first implementation.  It is here to measure its
    two failure modes, not to be shipped.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0
        self.over = False

    def _on_line(self, code, line_number):
        if self.over:
            raise BudgetExceeded("still over budget")
        self.count += 1
        if self.count > self.limit:
            self.over = True
            raise BudgetExceeded(f"line budget {self.limit} exceeded")

    def install(self):
        sys.monitoring.use_tool_id(TOOL_ID, "blender_copilot_budget_naive")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.LINE, self._on_line)
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.LINE)

    def disarm(self):
        sys.monitoring.set_events(TOOL_ID, 0)
        try:
            sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(TOOL_ID)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

SPHERE = "bpy.ops.mesh.primitive_uv_sphere_add(segments=200, ring_count=200)"
SVD = "np.linalg.svd(np.random.rand(3000, 3000))"
REGEX = "re.match(r'(a+)+$', 'a'*26 + 'b')"

# key -> (category, budget kind, limit, source, timeout_s, note)
# "budget kind": filtered | naive | none | instr
CASES: dict[str, dict] = {
    "while_true_pass": dict(
        cat="pure-python loop", kind="filtered", limit=2000, timeout=8,
        src="while True: pass",
        note="one-line loop; LINE events must still re-fire each iteration",
    ),
    "while_true_body": dict(
        cat="pure-python loop", kind="filtered", limit=2000, timeout=8,
        src="i = 0\nwhile True:\n    i += 1",
        note="two-line canonical runaway",
    ),
    "deep_recursion": dict(
        cat="deep recursion", kind="filtered", limit=20000, timeout=8,
        src="import sys\nsys.setrecursionlimit(4_000_000)\n"
            "def f(n):\n    return f(n + 1)\nf(0)",
        note="interrupts before the C stack is exhausted?",
    ),
    "generator_loop": dict(
        cat="generator loop", kind="filtered", limit=2000, timeout=8,
        src="def g():\n    while True:\n        yield 1\nfor _ in g():\n    pass",
        note="resume events in the generator frame",
    ),
    "sleep_3s": dict(
        cat="blocking C call", kind="filtered", limit=5, timeout=12,
        src="import time\ntime.sleep(3)\nx = 1\ny = 2",
        note="single time.sleep; budget cannot fire inside it",
    ),
    "sleep_loop": dict(
        cat="blocking call in a loop", kind="filtered", limit=2000, timeout=8,
        src="import time\nwhile True:\n    time.sleep(0.005)",
        note="blocking calls with Python between them stay stoppable",
    ),
    "socket_block": dict(
        cat="blocking C call", kind="filtered", limit=5, timeout=8,
        src="import socket\na, b = socket.socketpair()\nb.recv(1)",
        note="no peer ever writes; blocking recv",
    ),
    "regex_backtrack": dict(
        cat="C-level CPU loop", kind="filtered", limit=5, timeout=15,
        src="import re\nre.match(r'(a+)+$', 'a'*26 + 'b')\nx = 1",
        note="catastrophic backtracking inside one C call (~2.2 s)",
    ),
    "numpy_single_svd": dict(
        cat="C-level CPU call", kind="filtered", limit=5, timeout=25,
        src=f"import numpy as np\n{SVD}\nx = 1",
        note="one LAPACK call, ~3 s",
    ),
    "numpy_loop": dict(
        cat="C call in a loop", kind="filtered", limit=2000, timeout=12,
        src="import numpy as np\na = np.random.rand(64, 64)\n"
            "while True:\n    a = a @ a",
        note="short C calls with Python between them",
    ),
    "bpy_ops_heavy": dict(
        cat="blocking C call", kind="filtered", limit=2, timeout=15, bpy=True,
        src=f"{SPHERE}\nx = 1\ny = 2",
        note="one bpy.ops C call, ~1.4 s",
    ),
    "bpy_ops_loop": dict(
        cat="bpy.ops in a loop", kind="filtered", limit=2000, timeout=12, bpy=True,
        src="while True:\n    bpy.ops.mesh.primitive_cube_add()",
        note="many short C calls, Python between each",
    ),
    "swallow_inner": dict(
        cat="exception swallowed", kind="filtered", limit=4, timeout=8,
        src="while True:\n    try:\n        x = 1\n        y = 2\n        z = 3\n"
            "    except BaseException:\n        pass",
        note="raise lands inside the try; loop header is outside it",
    ),
    "swallow_outer": dict(
        cat="exception swallowed", kind="filtered", limit=4, timeout=8,
        src="try:\n    while True:\n        x = 1\nexcept BaseException:\n    pass\n"
            "after = 'reached'",
        note="try wraps the whole loop: can the turn swallow the budget?",
    ),
    "finally_cleanup": dict(
        cat="unwind discipline", kind="filtered", limit=4, timeout=8,
        src="_seen = []\ntry:\n    while True:\n        pass\n"
            "finally:\n    _seen.append('finally-ran')",
        note="does model code's own finally run on a monitor raise?",
    ),
    "naive_scope": dict(
        cat="naive budget", kind="naive", limit=3, timeout=8,
        src="i = 0\nwhile True:\n    i += 1",
        note="unfiltered budget fires on the runner's own lines",
    ),
    "naive_recovery": dict(
        cat="naive budget", kind="naive_recovery", limit=3, timeout=8,
        src="i = 0\nwhile True:\n    i += 1",
        note="can recovery code run while a naive budget is armed?",
    ),
    "recovery_push": dict(
        cat="unwind discipline", kind="filtered", limit=6, timeout=12, bpy=True,
        src="bpy.context.scene.objects['Cube'].location.z = 5.0\n"
            "i = 0\nwhile True:\n    i += 1",
        note="mutate, run away, then the runner's finally pushes undo",
    ),
    "monitor_reentrancy": dict(
        cat="callback semantics", kind="filtered", limit=200000, timeout=8,
        src="i = 0\nwhile i < 100000:\n    i += 1",
        note="does the callback re-enter itself? (max_depth)",
    ),
    "swallow_wide_instr": dict(
        cat="exception swallowed", kind="instr", limit=30, timeout=8,
        src="while True:\n    try:\n" + "".join(
            f"        v{i} = {i}\n" for i in range(40)
        ) + "    except BaseException:\n        pass",
        note="deterministic INSTRUCTION trip inside the try, not at its edge",
    ),
    "budget_tamper": dict(
        cat="budget tampering", kind="filtered", limit=100, timeout=8,
        src="import sys\nsys.monitoring.set_events(2, 0)\ni = 0\nwhile True:\n"
            "    i += 1",
        note="model code disarms the monitoring budget with one stdlib call",
    ),
    "alarm_tamper": dict(
        cat="budget tampering", kind="alarm", seconds=1.0, timeout=8,
        src="import signal\nsignal.setitimer(signal.ITIMER_REAL, 0)\n"
            "while True: pass",
        note="model code disarms the wall-clock alarm with one stdlib call",
    ),
    "alarm_swallow": dict(
        cat="exception swallowed", kind="alarm", seconds=1.0, timeout=8,
        src="while True:\n    try:\n        pass\n    except BaseException:\n        pass",
        note="can the signal raise be swallowed by the model like the monitor's?",
    ),
    "alarm_swallow_wide": dict(
        cat="exception swallowed", kind="alarm", seconds=1.0, repeat=True,
        timeout=8,
        src="while True:\n    try:\n        s = 0\n        for i in range(200000):\n"
            "            s += i\n    except BaseException:\n        pass",
        note="repeating alarm, wide guarded body: swallowed and never terminates?",
    ),
    # --- wave 2: the holes wave 1 opened -----------------------------------
    "while_true_pass_instr": dict(
        cat="pure-python loop", kind="instr", limit=200000, timeout=8,
        src="while True: pass",
        note="LINE never fires here; does INSTRUCTION?",
    ),
    "while_true_body_instr": dict(
        cat="pure-python loop", kind="instr", limit=200000, timeout=8,
        src="i = 0\nwhile True:\n    i += 1",
        note="INSTRUCTION cost on the canonical runaway",
    ),
    "for_iter_int": dict(
        cat="pure-python loop", kind="filtered", limit=2000, timeout=8,
        src="for _ in iter(int, 1):\n    pass",
        note="tight generator-driven loop, LINE events",
    ),
    "swallow_inner_instr": dict(
        cat="exception swallowed", kind="instr", limit=2000, timeout=8,
        src="while True:\n    try:\n        x = 1\n        y = 2\n        z = 3\n"
            "    except BaseException:\n        pass",
        note="is the INSTRUCTION budget swallowed like the LINE one?",
    ),
    "thread_runaway": dict(
        cat="runaway in a thread", kind="filtered", limit=2000, timeout=8,
        src="import threading\ndone = []\ndef spin():\n    i = 0\n    while True:\n"
            "        i += 1\nt = threading.Thread(target=spin)\nt.start()\n"
            "t.join()\ndone.append('joined')",
        note="global events fire in every thread; the raise lands in the worker",
        thread=True,
    ),
    "alarm_while_pass": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=8,
        src="while True: pass",
        note="does SIGALRM break a loop the LINE monitor cannot see?",
    ),
    "alarm_sleep": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=12,
        src="import time\ntime.sleep(5)\nx = 1",
        note="does SIGALRM interrupt a blocking sleep?",
    ),
    "alarm_socket": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=8,
        src="import socket\na, b = socket.socketpair()\nb.recv(1)",
        note="does SIGALRM interrupt a blocking recv?",
    ),
    "alarm_numpy": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=25,
        src=f"import numpy as np\n{SVD}\nx = 1",
        note="alarm during one long C call: deferred to the next bytecode?",
    ),
    "alarm_bpy_ops": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=12, bpy=True,
        src=f"{SPHERE}\nx = 1\ny = 2",
        note="alarm during one long Blender C operator",
        push=True,
    ),
    "alarm_deep_recursion": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=8,
        src="import sys\nsys.setrecursionlimit(4_000_000)\n"
            "def f(n):\n    return f(n + 1)\nf(0)",
        note="pure-Python recursion under the alarm",
    ),
    "alarm_generator_loop": dict(
        cat="wall-clock alarm", kind="alarm", seconds=1.0, timeout=8,
        src="def g():\n    while True:\n        yield 1\nfor _ in g():\n    pass",
        note="generator loop under the alarm",
    ),
}

HANDLER_SRC = "bpy.app.handlers.depsgraph_update_post.append(_cb)"

OVERHEAD_ITERS = 400_000
OVERHEAD_SRC = "x = 0\nfor i in range(%d):\n    x += i" % OVERHEAD_ITERS

CALIBRATE_LIMIT = 2_000_000
CALIBRATE_SRC = "i = 0\nwhile True:\n    i += 1"


# ---------------------------------------------------------------------------
# Child
# ---------------------------------------------------------------------------


def _emit(**kw) -> None:
    print("BC|" + json.dumps(kw, default=str), flush=True)


def _run_plain(case: dict) -> None:
    """exec under a filtered budget; report whether the runner's finally ran."""
    bpy = None
    ns: dict = {}
    if case.get("bpy"):
        import bpy as _bpy  # noqa: PLC0415

        bpy = _bpy
        ns["bpy"] = bpy
    budget = (FilteredInstructionBudget if case["kind"] == "instr"
              else FilteredLineBudget)(case["limit"])
    code = compile(case["src"], MODULE, "exec")
    state = {"excepted": None, "finally_ran": False, "after_exec": False,
             "push": None, "undo": None, "value_at_interrupt": None}
    t0 = time.monotonic()
    budget.install()
    try:
        try:
            exec(code, ns)
            state["after_exec"] = True
        except BudgetExceeded as exc:
            state["excepted"] = type(exc).__name__
        finally:
            budget.disarm()
            state["finally_ran"] = True
            if case["key"] == "recovery_push":
                try:
                    obj = bpy.context.scene.objects.get("Cube")
                    state["value_at_interrupt"] = (
                        None if obj is None else round(obj.location.z, 3)
                    )
                except Exception as exc:  # noqa: BLE001
                    state["value_at_interrupt"] = f"<{type(exc).__name__}>"
                try:
                    ret = bpy.ops.ed.undo_push(message="copilot: probe turn")
                    state["push"] = str(ret)
                except Exception as exc:  # noqa: BLE001
                    state["push"] = f"raised {type(exc).__name__}: {exc}"
                try:
                    ret = bpy.ops.ed.undo()
                    state["undo"] = str(ret)
                except Exception as exc:  # noqa: BLE001
                    state["undo"] = f"raised {type(exc).__name__}: {exc}"
                try:
                    obj = bpy.context.scene.objects.get("Cube")
                    state["value_after_undo"] = (
                        None if obj is None else round(obj.location.z, 3)
                    )
                except Exception as exc:  # noqa: BLE001
                    state["value_after_undo"] = f"<{type(exc).__name__}>"
    finally:
        elapsed = time.monotonic() - t0
        extra = {k: v for k, v in state.items() if k != "finally_ran"}
        extra["finally_ran"] = state["finally_ran"]
        extra["budget_count"] = budget.count
        extra["budget_over_flag"] = budget.over
        extra["callback_max_depth"] = budget.max_depth
        if case["key"] == "finally_cleanup":
            extra["model_seen"] = ns.get("_seen")
        if case["key"] == "swallow_outer":
            extra["after_marker"] = ns.get("after")
        if case["key"] == "monitor_reentrancy":
            extra["iters"] = ns.get("i")
        outcome = "interrupted" if state["excepted"] else (
            "completed" if state["after_exec"] else "unknown"
        )
        if state["excepted"] and state.get("after_exec"):
            outcome = "interrupted"
        if not state["excepted"] and state["after_exec"] and budget.over:
            outcome = ("thread-tripped, main survived" if case.get("thread")
                       else "swallowed-budget-flag-set")
        _emit(case=case["key"], outcome=outcome, elapsed=round(elapsed, 3),
              **extra)


def _run_alarm(case: dict) -> None:
    """Wall-clock budget: one SIGALRM whose handler raises in the main thread.

    This is the only mechanism probed here that can fire while the main thread
    is in a tight loop that emits no LINE events, because CPython checks pending
    signals at the eval breaker on every backward jump.
    """
    bpy = None
    ns: dict = {}
    if case.get("bpy"):
        import bpy as _bpy  # noqa: PLC0415

        bpy = _bpy
        ns["bpy"] = bpy
    seconds = case["seconds"]

    def _on_alarm(signum, frame):
        raise BudgetExceeded(f"wall-clock budget {seconds}s exceeded")

    code = compile(case["src"], MODULE, "exec")
    state = {"excepted": None, "after_exec": False, "finally_ran": False,
             "push": None, "value_at_interrupt": None,
             "value_after_undo": None}
    t0 = time.monotonic()
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds,
                     seconds if case.get("repeat") else 0)
    try:
        try:
            exec(code, ns)
            state["after_exec"] = True
        except BudgetExceeded as exc:
            state["excepted"] = type(exc).__name__
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            state["finally_ran"] = True
            if bpy is not None:
                try:
                    obj = bpy.context.scene.objects.get("Cube")
                    state["value_at_interrupt"] = (
                        None if obj is None else round(obj.location.z, 3)
                    )
                except Exception as exc:  # noqa: BLE001
                    state["value_at_interrupt"] = f"<{type(exc).__name__}>"
                try:
                    state["push"] = str(
                        bpy.ops.ed.undo_push(message="copilot: alarm probe turn")
                    )
                except Exception as exc:  # noqa: BLE001
                    state["push"] = f"raised {type(exc).__name__}: {exc}"
    finally:
        elapsed = time.monotonic() - t0
        outcome = "interrupted" if state["excepted"] else (
            "completed" if state["after_exec"] else "unknown"
        )
        _emit(case=case["key"], outcome=outcome, elapsed=round(elapsed, 3),
              seconds=seconds, **state)


def _run_count_events(event_name: str) -> None:
    """Count LINE or INSTRUCTION events fired by a bare `while True: pass`.

    Two variants exist because the answer differs by *how the loop was
    compiled*: `count_*` runs the loop as a statement of a normal function,
    `count_*_exec` runs it as an `exec`'d module code object -- the shape the
    real budget actually sees.  The loop never returns, so a sampler thread
    emits the count after a fixed window and hard-exits the child.
    """
    exec_mode = event_name.endswith("_exec")
    base = event_name.replace("_exec", "")
    import threading  # noqa: PLC0415

    m = sys.monitoring
    counter = {"n": 0}
    tool = 3 if base == "line" else 4
    event = m.events.LINE if base == "line" else m.events.INSTRUCTION

    def _cb(code, arg):
        counter["n"] += 1

    started = {"t": None}

    def _sampler():
        time.sleep(2.0)
        _emit(case=f"count_{event_name}", outcome="measured",
              seconds=round(time.monotonic() - started["t"], 2),
              events=counter["n"],
              events_per_sec=round(counter["n"] / 2.0))
        os._exit(0)

    threading.Thread(target=_sampler, daemon=True).start()
    time.sleep(0.2)
    m.use_tool_id(tool, f"bc_count_{event_name}")
    m.register_callback(tool, event, _cb)
    started["t"] = time.monotonic()
    m.set_events(tool, event)
    if exec_mode:
        exec(compile("while True: pass", MODULE, "exec"), {})
    while True:  # noqa: PIE790  -- the subject of the measurement
        pass


def _run_handler_outliving() -> None:
    """A callback the turn registers fires after the turn, unmonitored."""
    import bpy  # noqa: PLC0415

    ns: dict = {"bpy": bpy}
    fired = {"n": 0, "x": None}

    def _cb(*_a, **_k):
        fired["n"] += 1
        try:
            obj = bpy.context.scene.objects.get("Cube")
            if obj is not None:
                obj.location.x = 42.0
                fired["x"] = round(obj.location.x, 2)
        except Exception as exc:  # noqa: BLE001
            fired["x"] = f"<{type(exc).__name__}>"

    ns["_cb"] = _cb
    budget = FilteredLineBudget(50)
    code = compile(HANDLER_SRC, MODULE, "exec")
    state: dict = {}
    budget.install()
    try:
        exec(code, ns)
        state["registered"] = True
    except BudgetExceeded:
        state["registered"] = "budget"
    finally:
        budget.disarm()
    state["budget_armed_after_turn"] = bool(
        sys.monitoring.get_events(TOOL_ID)
    )
    try:
        state["push"] = str(
            bpy.ops.ed.undo_push(message="copilot: handler probe turn")
        )
    except Exception as exc:  # noqa: BLE001
        state["push"] = f"raised {type(exc).__name__}: {exc}"
    before = fired["n"]
    try:
        bpy.ops.mesh.primitive_cube_add()
        state["trigger"] = "ok"
    except Exception as exc:  # noqa: BLE001
        state["trigger"] = f"raised {type(exc).__name__}: {exc}"
    state["handler_fired_after_turn"] = fired["n"] - before
    state["cube_x_mutated_by_handler"] = fired["x"]
    state["turn_lines_charged"] = budget.count
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_cb)
    except Exception:  # noqa: BLE001
        pass
    _emit(case="handler_outliving", outcome="measured", **state)


def _run_naive(case: dict) -> None:
    budget = NaiveLineBudget(case["limit"])
    code = compile(case["src"], MODULE, "exec")
    out = {"excepted": None, "escaped": None, "finally_ran": False,
           "push": None}
    t0 = time.monotonic()
    budget.install()
    try:
        try:
            exec(code, {})
        except BudgetExceeded as exc:
            out["excepted"] = type(exc).__name__
    except BaseException as exc:  # noqa: BLE001
        out["escaped"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        budget.disarm()
        out["finally_ran"] = True
        _emit(case=case["key"], outcome="interrupted" if out["excepted"] else "other",
              elapsed=round(time.monotonic() - t0, 3), **out,
              budget_count=budget.count)


def _run_naive_recovery(case: dict) -> None:
    """After a naive interrupt, does ordinary recovery code survive?"""
    budget = NaiveLineBudget(case["limit"])
    code = compile(case["src"], MODULE, "exec")
    out: dict = {"excepted": None, "recovery": None, "finally_ran": False}
    t0 = time.monotonic()
    budget.install()
    try:
        try:
            exec(code, {})
        except BudgetExceeded as exc:
            out["excepted"] = type(exc).__name__
            try:
                marker = 0
                marker += 1  # recovery-shaped Python
                out["recovery"] = "ran"
            except BudgetExceeded:
                out["recovery"] = "re-raised"
        except BaseException as exc:  # noqa: BLE001
            out["recovery"] = f"escaped-during-handler: {type(exc).__name__}"
    finally:
        budget.disarm()
        out["finally_ran"] = True
        _emit(case=case["key"], outcome="measured",
              elapsed=round(time.monotonic() - t0, 3), **out,
              budget_count=budget.count)


def _run_overhead() -> None:
    code = compile(OVERHEAD_SRC, MODULE, "exec")
    results = {}

    def timeit(install=None, disarm=None, label="none"):
        holder = {"events": 0}

        def cb(c, line):
            holder["events"] += 1

        if install:
            install(cb)
        t0 = time.monotonic()
        exec(code, {})
        dt = time.monotonic() - t0
        if disarm:
            disarm()
        results[label] = dict(seconds=round(dt, 4), events=holder["events"])

    timeit()
    m = sys.monitoring

    def install_line(cb):
        m.use_tool_id(TOOL_ID, "bench_line")
        m.register_callback(TOOL_ID, m.events.LINE, cb)
        m.set_events(TOOL_ID, m.events.LINE)

    def disarm():
        m.set_events(TOOL_ID, 0)
        m.register_callback(TOOL_ID, m.events.LINE, None)
        try:
            m.free_tool_id(TOOL_ID)
        except Exception:
            pass

    timeit(install_line, disarm, "line")

    def install_instr(cb):
        m.use_tool_id(TOOL_ID, "bench_instr")
        m.register_callback(TOOL_ID, m.events.INSTRUCTION, cb)
        m.set_events(TOOL_ID, m.events.INSTRUCTION)

    timeit(install_instr, disarm, "instruction")
    base = results["none"]["seconds"]

    sig = {"n": 0}

    def _noop(signum, frame):
        sig["n"] += 1

    signal.signal(signal.SIGALRM, _noop)
    signal.setitimer(signal.ITIMER_REAL, 0.001, 0.001)
    t0 = time.monotonic()
    exec(code, {})
    dt = time.monotonic() - t0
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    results["alarm_1ms"] = dict(seconds=round(dt, 4), events=sig["n"],
                                slowdown=round(dt / base, 2))
    for k, v in results.items():
        v["slowdown"] = round(v["seconds"] / base, 2)
        if v["events"]:
            v["ns_per_event"] = round(v["seconds"] * 1e9 / v["events"], 1)
    _emit(case="overhead", outcome="measured", iterations=OVERHEAD_ITERS, **results)


def _run_calibrate() -> None:
    budget = FilteredLineBudget(CALIBRATE_LIMIT)
    code = compile(CALIBRATE_SRC, MODULE, "exec")
    t0 = time.monotonic()
    budget.install()
    try:
        exec(code, {})
    except BudgetExceeded:
        pass
    finally:
        budget.disarm()
    dt = time.monotonic() - t0
    _emit(case="calibrate", outcome="measured", limit=CALIBRATE_LIMIT,
          seconds=round(dt, 4), count=budget.count,
          lines_per_sec=round(budget.count / dt) if dt else None)


def child_main() -> int:
    key = os.environ[CHILD_ENV]
    if key == "overhead":
        _run_overhead()
        return 0
    if key == "calibrate":
        _run_calibrate()
        return 0
    if key == "count_line":
        _run_count_events("line")
        return 0
    if key == "count_instr":
        _run_count_events("instr")
        return 0
    if key == "count_line_exec":
        _run_count_events("line_exec")
        return 0
    if key == "count_instr_exec":
        _run_count_events("instr_exec")
        return 0
    if key == "handler_outliving":
        _run_handler_outliving()
        return 0
    case = CASES[key]
    case["key"] = key
    kind = case["kind"]
    if kind in ("filtered", "instr"):
        _run_plain(case)
    elif kind == "alarm":
        _run_alarm(case)
    elif kind == "naive":
        _run_naive(case)
    elif kind == "naive_recovery":
        _run_naive_recovery(case)
    else:
        raise SystemExit(f"unknown budget kind {kind!r}")
    return 0


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

ORDER = list(CASES) + ["overhead", "calibrate", "count_line", "count_instr",
                      "count_line_exec", "count_instr_exec", "handler_outliving"]


def driver_main() -> int:
    blender = sys.modules["bpy"].app.binary_path
    here = os.path.abspath(__file__)
    rows = []
    print(f"# runaway probe | Blender {sys.modules['bpy'].app.version_string} | "
          f"python {sys.version.split()[0]} | {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# driver: {blender} --factory-startup -b -P {here}")
    for key in ORDER:
        case = CASES.get(key, {})
        timeout = case.get("timeout", 30)
        env = dict(os.environ, **{CHILD_ENV: key})
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [blender, "--factory-startup", "-b", "-P", here],
                env=env, capture_output=True, text=True, timeout=timeout,
            )
            wall = time.monotonic() - t0
            payload = None
            noise = []
            for line in proc.stdout.splitlines():
                if line.startswith("BC|"):
                    payload = json.loads(line[3:])
                elif line.strip() and "Blender" not in line and "Read prefs" not in line:
                    noise.append(line)
            if payload is not None:
                verdict = payload.get("outcome", "?")
                detail = {k: v for k, v in payload.items()
                          if k not in ("case", "outcome")}
                rows.append(dict(case=key, verdict=verdict, wall=round(wall, 2),
                                 detail=detail))
                print(f"{key:<20} | {verdict:<28} | wall {wall:6.2f}s | "
                      f"{json.dumps(detail, default=str)}")
            else:
                tail = proc.stderr.strip().splitlines()[-1:] or ["<no stderr>"]
                rows.append(dict(case=key, verdict="crashed/escaped",
                                 wall=round(wall, 2),
                                 detail=dict(rc=proc.returncode, stderr_tail=tail[0])))
                print(f"{key:<20} | {'crashed/escaped':<28} | wall {wall:6.2f}s | "
                      f"rc={proc.returncode} {tail[0][:150]}")
        except subprocess.TimeoutExpired:
            wall = time.monotonic() - t0
            rows.append(dict(case=key, verdict="HUNG (killed)",
                             wall=round(wall, 2), detail=dict(timeout_s=timeout)))
            print(f"{key:<20} | {'HUNG (killed)':<28} | wall {wall:6.2f}s | "
                  f"no BC line within {timeout}s")
    out_path = os.path.join(os.path.dirname(here), "runaway_probe_output.json")
    with open(out_path, "w") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"# raw rows written to {out_path}")
    return 0


if __name__ == "__main__":
    if os.environ.get(CHILD_ENV):
        raise SystemExit(child_main())
    raise SystemExit(driver_main())
