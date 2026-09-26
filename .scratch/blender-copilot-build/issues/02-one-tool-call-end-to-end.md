# 02: One tool call, end to end

**What to build:** the thing the whole effort exists for. Type "make the cube
taller" into the panel and have it actually happen: the model asks to run code, the
code executes inside Blender with the live scene, the panel shows the call and its
output, and the model's closing reply arrives. This is the tracer bullet — it cuts
the loop, the tool surface, the worker and the panel in one piece.

**Blocked by:** 01 (Remove the dead paging code)

**Status:** open
**Triage:** ready-for-agent

- [ ] With a cube in the default scene, asking for it to be made taller changes the cube, with no hand-written code.
- [ ] The panel shows a tool row naming what ran, with its output behind the expander.
- [ ] Code runs with a fresh namespace per call: a name defined in one call is not visible to the next.
- [ ] A failing call returns the full traceback, and the model receives it on the following round.
- [ ] At most one tool call executes per timer tick, so the panel keeps repainting while a turn is in flight.
- [ ] The turn stops at the round and call caps and *reports* what it did rather than unwinding it.
- [ ] Nothing retries automatically, and a malformed call is fed back to the model as a continuation rather than a retry.
- [ ] Stop mid-turn leaves a history the provider will accept, so a turn can always be sent again.

**Context:** the loop's control flow and failure policy were decided in *The agent
loop's control flow and failure policy*; the tool's envelope, its fresh-namespace
rule and its always-return-the-traceback rule in *The three tools' contracts*.
Follow those rather than re-deciding them.
