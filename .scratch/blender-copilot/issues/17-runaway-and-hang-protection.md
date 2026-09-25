# Runaway and hang protection: what can actually be stopped

Type: prototype
Status: open
Blocked by: none

## Question

Two tickets leave the same hole open and neither closes it.

*What replaces undo as the recovery mechanism?* proposes a per-turn budget
enforced with `sys.monitoring` (`LINE`/`INSTRUCTION`, verified present) and
records it as **unbuilt and unprobed**, noting it "cannot stop a blocking C call,
so a hang remains possible and the UI must not claim otherwise".

*The capability boundary for model-authored code* §5 then states that the
capability guard **does not and cannot time-bound** `while True:` or a blocking C
call, and that "the UI must keep saying a hang is force-quit-only".

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
