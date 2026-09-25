# Runaway and hang protection: what can actually be stopped

Type: prototype
Status: claimed
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

<!-- recorded on resolution; not written at chart time -->
