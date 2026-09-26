# 06: Context projection

**What to build:** long conversations that keep working. When the history no longer
fits, the old material is dropped in a way the model can still reason about, a
fresh summary of the live scene rides along, and the panel says when trimming is in
effect — so the agent never becomes quietly forgetful.

**Blocked by:** 02 (One tool call, end to end)

**Status:** open
**Triage:** ready-for-agent

- [ ] A conversation long enough to exceed the budget still gets answered correctly.
- [ ] Old tool output is elided before whole turns are dropped, and eviction is by cost times reconstructibility rather than age.
- [ ] The live scene summary is the **last** message and the stable prompt stays first, so the provider's cache prefix survives a turn.
- [ ] A dropped turn leaves a visible marker, so a later reader can tell a gap from silence.
- [ ] The panel shows when trimming is in effect, and expanding that says which turns went.
- [ ] The stored transcript is **unchanged** by trimming — the record keeps everything the model no longer sees.
- [ ] The budget is derived from the provider's real limit rather than the assumed floor the design was written against.

**Context:** *How a conversation degrades as context grows* fixes the eviction
order, the byte-based measurement and the trailing-summary placement, and
*The first live send against DeepSeek* **confirmed** that a trailing system message
is accepted — so the fallback that ticket documented is not needed. The provider
offers a far larger context than the design assumed, so treat the current budget as
a floor to revisit, not a target.
