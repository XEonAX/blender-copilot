"""How long model-authored code may run, and what can honestly be said about it.

The whole rule lives here, and this module imports no `bpy`: the two clocks, the
signal, the verdict and every sentence the user or the model reads. `execution.py`
opens a call window around `exec`, and `conversation.py` opens the turn clock and
decides what a verdict means for the turn.

**The design is transcribed from *Runaway and hang protection*, which measured it
rather than argued it** (raw run: `tools/runaway_probe_output.json`, 43 cases,
Blender 5.2.2). What that ticket established, and what this file therefore does:

  * **Unit: wall-clock seconds, enforced by a repeating `SIGALRM` itimer.**
    **15 s per `run_blender_python` call, 60 s cumulative per user turn.** Lines
    and instructions were rejected on measurement - a `LINE` budget does not even
    stop `while True: pass` (both the jump and its target are line 1, so no line
    *changes*: 6 events in 1.8 s against 20.3 M for the same loop written as a
    statement), and instruction counting costs 11.4x on all Python while armed
    for no coverage the alarm does not already have. The alarm's own overhead is
    1.04x (~13 us per signal).
  * **Repeating, not one-shot.** A one-shot alarm that the code swallows is gone:
    the repeating interval re-interrupts on the next tick, so a caught raise buys
    one tick rather than the rest of the call. Measured: the swallow case is
    `HUNG` under one-shot designs and under `sys.monitoring` entirely.
  * **Why it can stop a loop, a sleep and a blocked read.** Python runs signal
    handlers on the main thread between bytecodes, so the raise lands even inside
    a tight loop; `time.sleep` and a blocking `socket.recv` are interrupted at
    1.00 s, because CPython's own wrappers are signal-aware. That is why the panel
    may no longer say "a blocking C call never stops" - it was measured false for
    exactly the two blocking calls a scene script reaches for first.
  * **Why it cannot stop everything, in two different ways.** A single native call
    (LAPACK, a heavy `bpy.ops`) is only interrupted *once it returns*: 5.34 s
    elapsed for a 1 s budget on a 3000x3000 SVD. And a `try/except BaseException`
    around the work swallows the raise, as does one line of `signal.setitimer`
    or `signal.signal`. Both were measured `HUNG` under every mechanism tried.

So the honest split is three-way, not two-way: **stopped**, **stopped late**, and
**not stoppable at all** - and the last one is stated rather than implied, because
the parts of it that can be *closed* cannot be closed from inside the process. The
code that can disarm the budget is the code being bounded; nothing stands in front
of an `import signal`, and the capability guard that would have was rejected and
never built. What this module can do, and does, is **notice**: the timer's state
and the handler identity are read back after the call, so a disarmed budget is
reported as disarmed rather than as a call that calmly used no time.

The one thing that is deliberately *not* here is a `Stop` request. Ticket 17's
Stop table says what the click does while a call runs - "nothing: the click is not
delivered while the main thread runs; the alarm ends the run instead" - and a
request flag nobody can deliver would be machinery pretending to be a lever. What
Stop can end, it already ends: the stream, and the rest of a turn whose queued
calls have not started yet.
"""

from __future__ import annotations

import signal
import threading
import time

# Ticket 17 §4, transcribed. The heaviest *legitimate* calls measured were 5.3-7 s
# (a 3000^2 SVD) and 1.4 s (a 200 000-vertex UV sphere), so 15 s admits a
# deliberately expensive call; 60 s bounds a turn made of several such calls.
CALL_SECONDS = 15.0
TURN_SECONDS = 60.0

# The alarm's interval. A second is the granularity of "the code is running too
# long" that a person can feel, and it is also the re-raise period for the swallow
# case - short enough that swallowed code cannot run at full speed for long.
REPEAT_SECONDS = 1.0

# How late a raise may be before it stopped being an interrupt and became a
# notification that a native call had finished. Measured, not guessed: a tight
# Python loop is interrupted 0.005 s after its deadline (the signal is delivered
# at the next bytecode), while a LAPACK call that overruns a 1 s budget by 4.3 s
# only manages to raise once it has returned. Anything past a quarter of a second
# is a native call, and the panel's sentence for that case says so.
LATE_SECONDS = 0.25

KIND_CALL = "call"
KIND_TURN = "turn"

# The permanent note under the Stop button while a call runs. It is drawn *before*
# the call starts and cannot be repainted while it runs, so it has to carry the
# whole contract up front - including what to do when the code does not stop,
# because by then the panel is frozen and this text is all the user has.
#
# Both directions, per this ticket's honesty requirement, and no claim that was
# not measured:
#   * a pure-Python loop, a sleep and a blocked read DO stop (measured at 1.00 s);
#   * a single long native call does NOT, until it returns (5.34 s for a 1 s
#     budget);
#   * Stop is not delivered while the code runs (the main thread is the code);
#   * a raise can be swallowed, and the timer can be switched off.
RUNNING_LINES = (
    # "of code" is not padding: the turn's figure is the sum of the call windows,
    # and the reading it replaced - "60s per turn" meaning the whole turn, thinking
    # included - is what a live run was refused under.
    f"Budget: {CALL_SECONDS:.0f}s per call, {TURN_SECONDS:.0f}s of code per turn.",
    "A Python loop, a sleep or a blocked read stops at the budget.",
    "A single Blender or NumPy call only stops once it returns.",
    "Stop cannot be delivered while the call is running.",
    "A call that swallows the interrupt, or switches the budget off, cannot be",
    "stopped: force-quit Blender, and this turn's changes may not be undoable.",
)

# The stop note's marks. Same vocabulary ticket 17 §3 uses, so the sentences the
# ticket printed and the sentences on screen are recognisably the same contract.
MARK_STOPPED = "\u23f1"
MARK_WARNING = "\u26a0"


class Exceeded(BaseException):
    """The budget raise. `BaseException`, deliberately.

    A `RuntimeError` would be caught by the ordinary `except Exception` that model
    code writes around its own work, which would turn the one thing in the process
    that is *supposed* to keep running into a line a model can swallow by accident.
    Subclassing `BaseException` puts it beside `KeyboardInterrupt` and `SystemExit`
    - interrupts, not errors - and the `try/except BaseException` case that still
    catches it is one of the three things this module documents as unstoppable.
    """

    def __init__(self, kind: str, seconds: float, limit: float, interrupts: int = 1):
        # `limit` is ≤ 0 for a call refused *at the door* - the turn has already
        # overspent, so there was no budget to arm - and that sign is load-bearing:
        # `_record`'s `late` rule reads it as "the alarm was never armed, so no
        # interrupt can have been delivered late". It is not, however, fit to print.
        # A live run (`tools/live_turn_probe.py`, 2026-09-26) produced the sentence
        # "the turn budget is used up: 7.27s of -7.27s" - in the panel and in the
        # model's own tool result - which tells a reader nothing and a model less.
        # The number is real, the pairing was not: when there is no budget left, the
        # honest sentence says so and gives the overspend.
        super().__init__(
            f"the {kind} budget is used up: {seconds:.2f}s of {limit:.2f}s"
            if limit > 0
            else f"the {kind} budget is already spent (overspent by {seconds:.2f}s)"
        )
        self.kind = kind
        self.seconds = seconds
        self.limit = limit
        self.interrupts = interrupts


class _Call:
    """One call's window: arm on entry, disarm and read back on exit.

    A context manager rather than bare calls in `run_python` because the read-back
    in `__exit__` is the part that must not be skipped on an exception path - the
    whole point of it is that the interrupting path is the normal one.
    """

    def __init__(self, limits: "Limits"):
        self.limits = limits

    def __enter__(self) -> "_Call":
        self.limits._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.limits._close(exc)
        return False


class Limits:
    """The two clocks, the alarm, and the verdict for the last call.

    One instance per process in the running addon (`LIMITS`), and as many as a
    check wants under `tests/`. `call_seconds` and `poll` are constructor arguments
    for that reason: a check that has to sleep fifteen seconds to prove the budget
    works is a check that gets deleted.
    """

    def __init__(
        self,
        call_seconds: float = CALL_SECONDS,
        turn_seconds: float = TURN_SECONDS,
        poll: float = REPEAT_SECONDS,
        clock=None,
    ):
        self.call_seconds = float(call_seconds)
        self.turn_seconds = float(turn_seconds)
        self.poll = float(poll)
        self._clock = clock or time.monotonic
        self._turn_started: float | None = None
        # Seconds of *code* run so far this turn - the sum of the call windows, not
        # the wall clock since the turn opened. See `turn_elapsed`.
        self._turn_used = 0.0
        self._call_started: float | None = None
        self._armed = False
        self._previous = None
        self._limit = 0.0
        self._kind = KIND_CALL
        self._verdict: dict = {}
        self.interrupts = 0

    # -- the turn ------------------------------------------------------------
    @property
    def turn_open(self) -> bool:
        return self._turn_started is not None

    @property
    def turn_elapsed(self) -> float:
        """Seconds this turn has spent **running code**, and nothing else.

        Wall clock from the turn's own start was the first implementation, and it
        was wrong: it charged the model's *thinking* - and every HTTP round trip -
        against a budget whose whole purpose is to bound model-authored code. Two
        live runs proved the difference rather than the principle
        (`tools/live_turn_probe.py`, 2026-09-26): 67 s and 79 s turns whose code
        added up to a few seconds were refused the call that would have finished the
        job, so the acceptance sentence failed twice with a half-built object, and
        the refusal sentence ("this turn's budget for running code is used up") was
        describing something that had not happened. The figure this module documents
        is "60 s cumulative per user turn" of the seconds the *alarm* can see - "60 s
        bounds a turn made of several such calls" (this file's header, transcribing
        ticket 17 §4) - so the accumulator is the call windows and the clock between
        them is not part of it.
        """
        return self._turn_used

    def begin_turn(self) -> bool:
        """A turn starts: its ledger opens. Only code time is added to it."""
        self._turn_started = self._clock()
        self._turn_used = 0.0
        self.interrupts = 0
        self._verdict = {}
        return True

    def end_turn(self) -> bool:
        """A turn is over. The last verdict is *kept* - it is what the panel's
        receipt and the stop note are built from - and only the ledger closes."""
        self._turn_started = None
        return True

    # -- one call ------------------------------------------------------------
    @property
    def armed(self) -> bool:
        return self._armed

    def allowance(self) -> tuple[float, str]:
        """`(seconds left, which budget is binding)` for the call about to run."""
        call_left = self.call_seconds
        if self._turn_started is None:
            # No turn open: a bare call still gets the per-call budget, and there
            # is no cumulative figure to compare against.
            return call_left, KIND_CALL
        turn_left = self.turn_seconds - self.turn_elapsed
        if turn_left < call_left:
            return turn_left, KIND_TURN
        return call_left, KIND_CALL

    def call(self) -> _Call:
        return _Call(self)

    def _open(self) -> None:
        self.interrupts = 0
        self._verdict = {}
        self._call_started = self._clock()
        remaining, kind = self.allowance()
        self._limit = remaining
        self._kind = kind
        if remaining <= 0:
            # Already over: raise before the code gets a bytecode, so a turn that
            # has spent its 60 s cannot be squeezed for one more call. The verdict
            # is written *here* rather than in `_close`, because an exception from
            # `__enter__` never reaches `__exit__` - and a spent turn whose ledger
            # stayed empty would be a turn that quietly kept going.
            self.interrupts = 1
            spent = Exceeded(kind, max(0.0, -remaining), remaining, interrupts=1)
            self._record(spent, armed=False)
            raise spent
        if not self._can_arm():
            # Off the main thread there is no SIGALRM to receive: `signal.signal`
            # raises there, and ticket 17 measured that the signal goes to the main
            # thread only. Nothing is armed, and the verdict says so rather than
            # reporting a clean call that was never bounded.
            return
        self._previous = signal.signal(signal.SIGALRM, self._handler)
        signal.setitimer(signal.ITIMER_REAL, min(remaining, self.poll), self.poll)
        self._armed = True

    def _close(self, exc: BaseException | None) -> None:
        # `_armed` first: a signal already delivered is run at the next bytecode
        # boundary, which can be *after* this call - so the handler has to be able
        # to tell that its window is closed and do nothing.
        was_armed = self._armed
        self._armed = False
        disarmed = False
        if was_armed:
            try:
                disarmed = self._disarmed()
            except Exception:  # noqa: BLE001 - a read-back must not mask the verdict
                disarmed = False
            signal.setitimer(signal.ITIMER_REAL, 0)
            if self._previous is not None:
                try:
                    signal.signal(signal.SIGALRM, self._previous)
                except Exception:  # noqa: BLE001 - same: never raise from here
                    pass
        # The turn's ledger, before the verdict erases the call's start. Charged on
        # every path out of the call - returned, raised, interrupted, disarmed -
        # because the seconds were spent either way, and a turn that only counted
        # the calls it finished cleanly would be a turn that could outlast its own
        # budget by dying repeatedly.
        if self._call_started is not None:
            self._turn_used += max(0.0, self._clock() - self._call_started)
        self._record(exc if isinstance(exc, Exceeded) else None, armed=was_armed, disarmed=disarmed)
        self._call_started = None

    def _record(self, stopped: "Exceeded | None", armed: bool, disarmed: bool = False) -> None:
        """The verdict for one call: what was measured, and nothing that was not.

        `late` is the one inference in here, and it is a measured one: a raise can
        only be delivered when the main thread next runs a bytecode, so a **single**
        raise that arrives long after its own deadline is a raise that waited for a
        native call to return. Both halves of that are load-bearing:

          * `interrupts == 1` - several raises mean the alarm was delivered on
            time, repeatedly, and the code swallowed it; the elapsed time is then
            the code's doing and not a native call's. Measured: the swallow-twice
            case in `tools/budget_probe.py` reaches 0.80 s against a 0.30 s budget
            with three raises, and the first version of this rule called that a
            late native call and said so on screen.
          * `limit > 0` - a budget already spent at the door was never armed, so
            there is no interrupt that could have been delivered late.
        """
        if stopped is None:
            seconds = max(0.0, self._clock() - (self._call_started or self._clock()))
            limit = self._limit
            kind = self._kind
        else:
            seconds = stopped.seconds
            limit = stopped.limit
            kind = stopped.kind
        self._verdict = {
            "armed": bool(armed),
            "kind": kind,
            "seconds": round(seconds, 3),
            "limit": round(limit, 3),
            "interrupts": self.interrupts,
            "interrupted": stopped is not None,
            "late": bool(
                stopped is not None
                and limit > 0
                and self.interrupts == 1
                and (seconds - limit) > LATE_SECONDS
            ),
            "disarmed": bool(disarmed),
        }

    def _can_arm(self) -> bool:
        try:
            return threading.current_thread() is threading.main_thread()
        except Exception:  # noqa: BLE001 - an unknown thread is treated as "no"
            return False

    def _disarmed(self) -> bool:
        """Whether our timer and handler are still ours, read back after the call.

        Two reads, because they are two different one-line escapes and only one of
        them shows up in the timer: `setitimer(ITIMER_REAL, 0)` clears it, and
        `signal.signal(SIGALRM, ...)` leaves it ticking into somebody else's
        handler. `getitimer` and `getsignal` both exist in the bundled 3.13 and in
        the 3.9 the CPython suite runs on (measured, not assumed).

        `==` and not `is`: a bound method is rebuilt on every attribute access
        (`a.m is a.m` is False, measured), so identity would report every call as
        disarmed.
        """
        if signal.getsignal(signal.SIGALRM) != self._handler:
            return True
        first, interval = signal.getitimer(signal.ITIMER_REAL)
        return first <= 0.0 and interval <= 0.0

    def _handler(self, _signum, _frame) -> None:
        """The alarm. Runs on the main thread between bytecodes, inside whatever
        the model's code was doing - which is the entire mechanism."""
        if not self._armed:
            # A late delivery for a call that has already ended. Doing nothing is
            # the only correct answer: raising here would land in the addon.
            return
        elapsed = self._clock() - (self._call_started or self._clock())
        if elapsed < self._limit:
            return
        self.interrupts += 1
        # Constructed here rather than in `_close` so the *raise site* carries the
        # elapsed time. A native call that returns late reports its own lateness
        # through this number, which is how "deferred" is distinguished from
        # "interrupted".
        raise Exceeded(self._kind, elapsed, self._limit, interrupts=self.interrupts)

    def verdict(self) -> dict:
        """The last call's outcome, or `{}` when no call has run since the reset."""
        return dict(self._verdict)


# One instance for the process, shared by the two halves that need it: the sandbox
# opens a call window on it (`execution.run_python`) and the loop opens and closes
# the turn on it (`conversation.begin_turn` / `_end_turn`). They must be the same
# object or the 60 s figure would be two 60 s figures, and `panel_draw_smoke.py`
# asserts that identity rather than trusting it.
LIMITS = Limits()


# ---------------------------------------------------------------------------
# What is said about a verdict
# ---------------------------------------------------------------------------

def verdict_note(verdict: dict) -> str:
    """The model-facing sentence for a call that was interrupted or not bounded.

    Goes into the tool result, which is the only place the model can learn that
    its code did not finish. The panel's sentences below are for the user; this one
    is technical, and says the thing that matters for what the model does next:
    the call did not complete, and part of its work may stand.
    """
    if not verdict:
        return ""
    if verdict.get("interrupted") and verdict.get("limit", 0) <= 0:
        # Refused at the door: the turn's budget was already spent when this call
        # started, so no code ran at all and saying "interrupted after 0.0s" would
        # be describing an interrupt that never happened.
        return (
            "No code ran: this turn's budget for running code is used up, so the "
            "call was refused before it started."
        )
    if verdict.get("interrupted"):
        return (
            f"The call was interrupted {verdict['seconds']:.1f}s in and did not "
            f"finish: the code ran past its {verdict['limit']:.0f}s budget and the "
            "turn was stopped. Anything it changed before the interrupt is still "
            "applied - read the scene before assuming a state."
        )
    if verdict.get("interrupts"):
        return (
            f"The code caught the budget interrupt and kept running "
            f"({verdict['interrupts']} raise(s) swallowed). The turn was stopped; "
            "do not rely on a budget to bound code that catches exceptions."
        )
    if verdict.get("disarmed"):
        return (
            "The code switched the budget off (`signal.setitimer` or "
            "`signal.signal`), so nothing bounded this call. The turn was stopped."
        )
    return ""


def stopped_sentence(verdict: dict) -> str:
    """What the panel says after an interrupted or unbounded call.

    Ticket 17 §3's sentences, transcribed where the measurement still supports
    them and split three ways because the measurements split three ways. The
    undo claim in §3's first sentence is *not* repeated here: the receipt box
    underneath already makes it, and it is the only part of the panel that knows
    whether Blender actually recorded a step (in edit mode it does not, and two
    contradicting sentences on one screen is the failure mode being avoided).
    """
    if not verdict:
        return f"{MARK_STOPPED} The call did not finish."
    seconds = f"{verdict['seconds']:.1f}s"
    if verdict.get("interrupted"):
        if verdict.get("late"):
            return (
                f"{MARK_STOPPED} Stopped after {seconds} \u2014 a single "
                "Blender/NumPy call cannot be interrupted once it has started."
            )
        if verdict.get("kind") == KIND_TURN:
            return (
                f"{MARK_STOPPED} Stopped after {seconds} \u2014 this turn has used "
                f"its {TURN_SECONDS:.0f}s of call time."
            )
        # Ticket 17 §3's sentence, verbatim, and deliberately without the limit
        # spelled out a second time: the permanent note above the switch already
        # carries the two figures, and a sentence that repeats them per call is how
        # the two copies start to disagree. What this line adds is the measurement -
        # how long the code actually got.
        return (
            f"{MARK_STOPPED} Stopped after {seconds} \u2014 the code ran past its "
            "budget."
        )
    if verdict.get("interrupts") and verdict.get("disarmed"):
        return (
            f"{MARK_WARNING} The code caught the interrupt and switched the budget "
            "off \u2014 nothing bounded that call. Stop cannot be delivered while "
            "the main thread runs. The turn stops here."
        )
    if verdict.get("disarmed"):
        return (
            f"{MARK_WARNING} The code switched the budget off \u2014 nothing bounded "
            "that call. The turn stops here."
        )
    if verdict.get("interrupts"):
        return (
            f"{MARK_WARNING} The code caught the interrupt and kept running "
            f"\u2014 the call ran {seconds} past its budget. The turn stops here."
        )
    return ""


def stopped(verdict: dict) -> bool:
    """Whether a verdict means "this call was not bounded by its budget".

    The loop's question, asked in one place so the panel and the loop cannot
    disagree about which verdicts stop a turn: an interrupt that took, a call
    refused because the turn's budget was already spent, a raise that was
    swallowed, and a timer that was switched off all count. A clean call inside its
    budget - the overwhelmingly common case - does not.

    Deliberately **not** gated on `verdict["armed"]`, which is what the first
    version did: a call refused at the door is never armed (there is no window to
    arm), so that gate made the one verdict that says "this turn has nothing left"
    the one verdict the loop ignored - an error row and another round, up to the
    caps, instead of a stop. A verdict whose flags are all false is already `False`
    here, which is the case `armed` was standing in for.
    """
    if not verdict:
        return False
    return bool(
        verdict.get("interrupted") or verdict.get("interrupts") or verdict.get("disarmed")
    )
