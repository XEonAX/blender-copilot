# Runaway and hang protection: what can actually be stopped

Type: prototype
Status: resolved
Blocked by: none

## Retry note, 2026-09-25 — read this before starting

**A previous instance was killed on this ticket after wedging itself four
separate times.** It is a hard ticket to work safely, because its subject is the
exact hazard that kills the agent probing it. Three things change for the retry.

**1. Its artifact survives, and it is good.** `tools/runaway_probe.py` — 531
lines, compiles clean — already implements the driver/child split this ticket's
method section asks for: with `BC_RUNAWAY_CASE` set it runs one case and prints a
`BC|{json}` line; without it, it re-invokes Blender once per case, *so an
unkillable hang is a killed child rather than a killed session*. **Reuse it
rather than rewriting it.**

**2. Bound every invocation.** The instance ran
`BC_RUNAWAY_CASE=while_true_pass ... Blender -b -P tools/runaway_probe.py`
*directly* — no driver, no deadline, and the case is by definition an infinite
loop. `while True: pass` never returns, so the tool call never returned, so the
instance could not advance. Four wedges, roughly 30 minutes lost, and each one a
headless Blender at 100% CPU that had to be killed from outside the session. Wrap
every run as `python3 tools/bounded_run.py 30 -- <cmd>`; a `BOUNDED | KILLED`
line is the **observation** for a hang case, not a failure.

**3. Non-headless is permitted for this ticket**, granted by the project owner.
Some cases need an event loop, and both `bpy.app.timers` and undo need a screen.
Launch in the background, bound it, capture to a file, and have the script quit
Blender itself. Take no screenshots — visual judgement stays the human's.

**The wedges produced evidence that answers part of §1, so it is not wasted:**
a plain Python infinite loop under `blender -b` is stopped by nothing in this
addon, and not by a `perl -e 'alarm …'` wrapper either (measured failing — see
`AGENTS.md`). That is the *unstoppable* column, observed rather than predicted,
and it cost real time to obtain.

## Question

Two tickets leave the same hole open and neither closes it.

*What replaces undo as the recovery mechanism?* proposes a per-turn budget
enforced with `sys.monitoring` (`LINE`/`INSTRUCTION`, verified present) and
records it as **unbuilt and unprobed**, noting it "cannot stop a blocking C call,
so a hang remains possible and the UI must not claim otherwise".

*The capability boundary for model-authored code* §5 then states that the
capability guard **does not and cannot time-bound** `while True:` or a blocking C
call, and that "the UI must keep saying a hang is force-quit-only".

> **Premise corrected 2026-09-25.** That second ticket's mechanism was **rejected
> and set aside** — there is no capability guard, so it is no longer a *guard
> limitation* that a hang cannot be bounded. The conclusion is unchanged and in
> fact sharper: **nothing at all** is preventing a hang, because nothing is
> installed. Read §5's finding as a fact about `sys.monitoring` and about blocking
> C calls, not as a fact about a guard that will not exist. One item this adds:
> with `bpy.app.handlers` and `bpy.app.timers` open to model-authored code, a turn
> can also register work that runs *outside* the turn — so "what can be stopped"
> has to cover a callback that fires after the turn has ended, and whether the
> turn-end undo push can reach it (it cannot reach it; say so plainly rather than
> discovering it later).

So the design currently ships with three unanswered questions and one honesty
requirement. This ticket answers them by building and measuring, not by
reasoning:

1. **Build the `sys.monitoring` budget and find its real boundary.** Which
   constructs does it actually interrupt — a tight `while True:`, `while True:
   pass`, a deeply recursive call, a generator loop, a `time.sleep(60)`, a
   long-running `bpy.ops` call, a numpy operation on a large array, a blocking
   socket read? For each: interrupted, or hung until force-quit? Report the
   measured answer per construct, since "cannot stop a blocking C call" is a
   claim about a whole category and the categories are not equally bad.
2. **What does interrupting actually cost?** Raising out of model-authored code
   mid-mutation leaves the scene partly changed. Does the turn-end push in
   *What replaces undo as the recovery mechanism?* §1 still fire when the
   exception is raised from a monitoring callback rather than from the code —
   i.e. does the `finally` discipline survive this specific interruption path?
   This is the interaction neither ticket examined and it is the one that decides
   whether the budget is a safety feature or a second way to corrupt state.
3. **The honesty contract.** Write the exact user-facing sentence for each
   observed category: stopped, and *by what*, versus not stoppable and therefore
   force-quit-only. The tickets require the UI to say so; nothing has specified
   what it says. Include what `Stop` does in each case, given *What replaces undo
   as the recovery mechanism?* and *How the addon talks to the API* between them
   already redefine Stop twice.
4. **The budget's unit and value.** Instructions, lines, wall-clock, or a
   combination — and the number, with the reasoning that produced it. A budget
   measured in instructions behaves very differently from one measured in
   seconds on a machine under load.

Method: a probe script under `tools/`, run headlessly against the installed
Blender 5.2.2 (`blender -b`), with one case per construct and the outcome
recorded as observed. Where a case can only be judged in the GUI, say so and
hand it to the human rather than guessing — but note that an unkillable hang is
the case least likely to be worth asking a human to sit through, so prefer
probing in a child process that can be killed and its fate recorded.

Deliverable: the probe, its output, and the per-construct table, plus an explicit
list of what remains unstoppable.

## Answer

Measured, not reasoned. Probe: `tools/runaway_probe.py` (driver/child split, one
Blender child per case, `BC|{json}` per case); raw run: `tools/runaway_probe_output.json`
(43 cases). Every invocation was wrapped in `tools/bounded_run.py`, so a case that
is by construction an infinite loop is a killed child and a `HUNG (killed)` line,
never a wedged session. Final run: 132 s, exit 0, no wedge. Blender 5.2.2 LTS,
Python 3.13.13.

```
python3 tools/bounded_run.py 500 -- \
  /Applications/Blender.app/Contents/MacOS/Blender --factory-startup -b \
  -P tools/runaway_probe.py
```

### Summary of the four questions

1. **A `sys.monitoring` budget is the wrong primary mechanism.** It cannot stop a
   blocking call (measured), it costs 4.4× (LINE) / 11.4× (INSTRUCTION) on all
   Python while armed, and — the surprise — LINE does not even stop
   `while True: pass`. A SIGALRM wall-clock alarm covers strictly more of the
   tested constructs at ~1.04× overhead and stops `sleep`, blocking `recv`, and
   `while True: pass`, none of which the monitor can touch. Ship the alarm.
2. **Interrupting costs nothing extra when the model lets it through**: the
   model's own `finally` runs, the runner's `finally` runs, and the turn-end
   undo push still returns `{'FINISHED'}` on both the monitor path and the signal
   path (`recovery_push`, `alarm_bpy_ops`). It costs everything when the model
   *catches* the exception: `exec` never returns, so the push never happens and
   the partial mutation is left unrecoverable.
3. **The honest contract is three sentences plus one running label**, below.
4. **Unit: wall-clock seconds**, 15 s per `run_blender_python` call and 60 s
   cumulative per turn. Lines and instructions are rejected with the numbers
   that killed them.

### §1 — per-construct measured boundary

`LINE`/`INSTR` = the filtered `sys.monitoring` budget from ticket *What replaces
undo as the recovery mechanism?* §6; `alarm` = one SIGALRM raised in the main
thread. "deferred" means the interrupt fired only once the C call returned.

| construct | LINE | INSTRUCTION | SIGALRM wall-clock |
|---|---|---|---|
| `while True: pass` (module-level one-liner) | **HUNG** | interrupted, 0.022 s | interrupted, 1.005 s |
| `i=0; while True: i+=1` | interrupted, <0.001 s | interrupted, 0.027 s | not run separately (same shape) |
| generator loop `for _ in g()` | interrupted, <0.001 s | — | interrupted, 1.001 s |
| deep recursion (`setrecursionlimit(4M)`) | interrupted at 20 001 lines, 0.006 s | — | **never fired**: exec self-terminated with `RecursionError` at 0.80 s and the process then took 20.3 s to tear down ~10⁶ frames |
| `time.sleep(3)` / `sleep(5)` | completed, 3.0 s (4 lines charged) | — | interrupted, 1.002 s |
| blocking `socket.recv` (no peer) | **HUNG** | — | interrupted, 1.005 s |
| catastrophic regex, ~2.2 s one C call | completed, 2.24 s (3 lines) | — | deferred class (see numpy) |
| `np.linalg.svd(3000×3000)`, ~5–7 s one call | completed, 3 lines | — | interrupted, but **deferred**: 5.34 s elapsed for a 1 s budget |
| one heavy `bpy.ops` (~1.4 s) | interrupted, 3 lines, after the op | — | interrupted, deferred: 1.409 s |
| `while True: bpy.ops.mesh.primitive_cube_add()` | **HUNG** at 12 s (line budget never reached) | — | would trip at the deadline (inferred from the row above) |
| runaway in a worker thread | tripped in the worker; `join` returned; main survived | — | — |
| `try/except BaseException` around the work | **HUNG** | **HUNG** (`swallow_wide_instr`) | **HUNG** even with a repeating 1 s alarm |
| model disarms the mechanism | **HUNG** (`sys.monitoring.set_events(2,0)`) | — | **HUNG** (`signal.setitimer(ITIMER_REAL,0)`) |

Why LINE misses `while True: pass` — this is not flakiness, it is the line table:

```
compile("while True: pass", "<copilot>", "exec").co_lines()
  (0, 2, 0)   RESUME
  (2, 6, 1)   JUMP_BACKWARD 2 (to itself)
```

Both the jump and its target are line 1, so no line *changes* and the LINE event
never repeats: counted **6 events in 1.8 s**, against 20.3 M events in 1.8 s for
the same loop written as a normal function statement. INSTRUCTION fires 24.2 M
times/s here and catches it. But INSTRUCTION buys nothing the alarm does not
already have for this case, and costs 11.4× against the alarm's 1.04×.

Overhead on a 400 000-iteration pure-Python loop (400 k iterations, 800 k lines,
2.8 M instructions): none 1.00× · alarm armed every 1 ms **1.04×** (~13 µs per
signal) · LINE **4.44×** (129 ns/event) · INSTRUCTION **11.39×** (95 ns/event).
Callback re-entrancy: max depth 1 (no storm).

### §2 — what interrupting costs

- **Model code's `finally` runs.** `finally_cleanup`: the raise from the monitor
  unwinds through the model frame, `_seen == ['finally-ran']`.
- **The runner's `finally` runs on both interrupt paths.** `recovery_push`
  (`except BudgetExceeded`, then) `finally_ran: true`, `push: {'FINISHED'}`;
  `alarm_bpy_ops` (signal raise) identically `push: {'FINISHED'}`. So the
  turn-end push in ticket 12 §1 **does** survive this specific interruption path.
- **But `ed.undo()` cannot be executed headlessly:** in `recovery_push` the push
  succeeded and `bpy.ops.ed.undo()` raised
  `RuntimeError: Operator bpy.ops.ed.undo.poll() failed, context is incorrect`,
  with `Cube.location.z` unchanged at 5.0. Whether Ctrl+Z then actually reverts
  the interrupted turn is therefore **still GUI-only** and handed to the human
  (same open item as *Confirm the four undo cases in a GUI*).
- **The swallow case is the second corruption path.** If the model catches the
  raise (`except BaseException`, or a bare `except:`), the disarm-before-raise
  design has already switched the monitor off, `exec` never returns, the runner's
  `finally` never runs, no push is made and no receipt is shown. Measured HUNG
  under LINE, under INSTRUCTION, and under a repeating alarm. `swallow_outer`
  shows the milder form: the budget trip was caught and **the turn kept running**
  past it (`after_marker: 'reached'`) — a caught budget does not stop the turn,
  it just stops counting.
- **Disarm-before-raise is forced, not a choice.** An always-armed budget cannot
  run recovery at all: `naive_scope` / `naive_recovery` die with
  `BudgetExceeded: still over budget` before their handler can execute. So the
  same latch that lets the push fire is the latch that lets the model swallow it.
  That trade is inherent to exception-based interruption; it is not fixable by
  tuning.
- **Post-turn work is outside everything.** `handler_outliving`: a turn registers
  a `depsgraph_update_post` handler, the turn ends, `undo_push` returns
  `{'FINISHED'}`, the budget is disarmed (`budget_armed_after_turn: false`), and a
  later mutation fires the handler once, which sets `Cube.location.x = 42.0`.
  That mutation has no receipt, no budget, and no turn step. Same for
  `bpy.app.timers`; the timer half needs the GUI event loop and was **not**
  probed — hand it to the human.
- A runaway in a **worker thread** is stopped (the budget raise lands in the
  worker; `join` returns and the main thread survives), but a thread blocked in
  native code is not: SIGALRM is delivered to the main thread only.

### §3 — the honesty contract (exact strings)

Persistent under the input (unchanged, still true):

> `Ctrl+Z undoes one agent turn. Changes outside this .blend — files, network, preferences — cannot be undone.`

While a tool call is executing — this replaces ticket 08's
`running code — cannot be interrupted`, which is no longer accurate:

> `running code — stops at its 15s budget; Stop cannot be delivered until it returns`

On a normal stop (loop, sleep, blocking read, generator, recursion):

> `⏱ Stopped after <n>s — the code ran past its budget. This turn's changes are one Ctrl+Z step.`

When only a long single native call could be stopped (the alarm deferred):

> `⏱ Stopped after <n>s — a single Blender/NumPy call cannot be interrupted once it has started.`

When the stop did not take (swallowed, or the mechanism was disabled):

> `⚠ The code is not stopping — it caught the interrupt, or switched the budget off. Stop cannot be delivered while the main thread runs. If it does not return, force-quit Blender; this turn may be partly applied and cannot be undone.`

After a turn that registered out-of-turn work:

> `This turn registered code that runs after it (a handler/timer). It is outside this turn's undo step and its receipt.`

Stop, per case (the third definition, reconciled with the two already in the map):

| while… | Stop does |
|---|---|
| streaming a reply | cancels the worker stream, then kills `_worker.py` after grace (ticket *How the addon talks to the API*) — real |
| `run_blender_python` executing | nothing: the click is not delivered while the main thread runs; the alarm ends the run instead |
| the code swallowed/disarmed the budget | nothing; force-quit only |

### §4 — unit and value

**Unit: wall-clock seconds, enforced by a repeating `SIGALRM` itimer.** Value:
**15 s per `run_blender_python` call, 60 s cumulative per user turn.** The heaviest
*legitimate* calls measured were 5.3–7 s (a 3000² SVD) and 1.4 s (a 200 000-vert
UV sphere), so 15 s admits a deliberately expensive call; 60 s bounds a turn made
of several such calls. A repeating 1 s interval is required, not a one-shot: if
the model swallows the raise, the next tick re-interrupts, so it cannot run at
full speed forever. Overhead ~13 µs/signal, 1.04× measured.

Secondary, cheap liveness guard: keep a filtered **LINE** budget as a backstop at
**20 000 000 lines per call** (≈2.9 s of tight pure-Python at the measured
6.9 M lines/s). It catches pure-Python progress that never reaches a signal
check, and it bounds forward-recursion stack growth far earlier than the
20 s `RecursionError` teardown measured above. It is not load-bearing on its own.

Rejected:
- **Lines as the primary unit** — measured to miss `while True: pass` entirely and
to be unable to bound wall-clock when one Python line is an expensive C call
(`bpy_ops_loop` HUNG past 12 s with a 2 000-line budget).
- **Instructions as the primary unit** — 11.39× on all Python while armed, still
  cannot stop `sleep`/`recv`, and the only case it wins (early detection of
  `while True: pass`) is already bounded by the alarm at the budget value.
- **Wall-clock + instructions as a "combination"** — the instruction monitor adds
  11× cost over LINE for zero coverage the LINE guard plus alarm does not already
  have, on every measured case.
- **Running model code in a killable subprocess** — not a budget choice but a
  pre-existing one: `bpy` is main-thread-only and live `bpy.context` is the point
  of the in-process design.

### What remains unstoppable — explicit list

1. `try/except BaseException` (or bare `except:`) around the work. Measured HUNG
   under all three mechanisms.
2. Model code calling `sys.monitoring.set_events(<id>, 0)` or
   `signal.setitimer(signal.ITIMER_REAL, 0)` — one line each, both measured HUNG.
   With the capability guard rejected and not built, nothing stands in front of
   either call.
3. A single native call that never returns — a deadlock, or a C extension loop
   that never reaches a Python signal check. The stoppable blocking calls are
   narrower than "blocking C call": `time.sleep` and `socket.recv` **are** stopped
   by the alarm (measured at 1.00 s).
4. A native call that returns late: only stopped after it returns (5.34 s elapsed
   for a 1 s alarm on LAPACK).
5. Anything registered for after the turn (`bpy.app.handlers`, `bpy.app.timers`):
   unmonitored, unpushed, unreceipted. Handler half measured; timer half GUI-only.
6. Work on a non-main thread blocked in native code: SIGALRM goes to main.

### Human-only, not guessed

- Whether Ctrl+Z actually reverts an interrupted turn (`ed.undo()` needs a GUI;
  no GUI was launched and no screenshot was taken). Ticket *Confirm the four undo
  cases in a GUI* owns this.
- `bpy.app.timers` behaviour after a turn ends.
- Ratification of the three §3 sentences and the 15 s / 60 s numbers before they
  are put on screen. The escalation policy is deliberate: the addon never
  auto-kills Blender, because the recovery `.blend` copy is off by default and a
  kill would discard unsaved work. That is a policy choice and should be ruled on
  rather than assumed.

