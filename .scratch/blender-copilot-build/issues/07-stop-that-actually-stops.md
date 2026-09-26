# 07: Stop that actually stops

**What to build:** Stop that does something. Today the panel says it cannot
interrupt running code, which is honest but weaker than the truth: some things can
be stopped and some cannot, and the panel should say which is which — and then
actually stop the ones that can be.

**Blocked by:** 02 (One tool call, end to end)

**Status:** open
**Triage:** ready-for-agent

- [ ] Running a pure-Python infinite loop and pressing Stop ends it.
- [ ] A long native call is **not** claimed to be stoppable, and the panel says so rather than pretending.
- [ ] The panel's wording matches what was actually measured, in both directions — it must not under-promise either.
- [ ] An interrupted turn still leaves a revertible undo step behind it.
- [ ] The budget has a per-call and a per-turn figure, and both are named in the panel's copy.
- [ ] Anything that can disarm the budget is either closed or stated plainly as disarmed.

**Context:** *Runaway and hang protection* **measured** this: a line-level monitor
cannot stop a plain infinite loop at all, a wall-clock signal stops the loop, a
sleep and a blocking read cheaply, a long native call is only stopped once it
returns, and the budget can be disarmed in a line by the code it is meant to bound.
Follow the measurements, not the aspiration, and keep the honesty claim in step
with them.
