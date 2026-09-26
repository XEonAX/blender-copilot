# 04: Persistence

**What to build:** conversations that survive. Close Blender, reopen the same file,
and the history is still there and still scoped to that file. Independent of the
tool loop, so this can run **in parallel with 02** rather than waiting for it.

**Blocked by:** None (can start immediately)

**Status:** open
**Triage:** ready-for-agent

- [ ] A conversation survives closing and reopening Blender on the same file.
- [ ] History is scoped per file path: opening a different file shows that file's conversation, not the previous one's.
- [ ] Save As re-scopes to the new path without silently duplicating or losing the old history.
- [ ] Nothing is ever stored inside the .blend itself.
- [ ] An unsaved file keeps its conversation for the session only, and says so.
- [ ] The cap prunes whole turns and never leaves a tool result orphaned from the call that produced it.
- [ ] Clearing starts a new conversation; deleting everything requires an explicit confirmation and offers to reveal the folder.
- [ ] Opening the file switches the scope, and the handler that does it survives a reload.

**Context:** *Where chat history lives* decided the location, the scope, the caps
and the pruning rule, and *verified* that a `Text` datablock persists into the
.blend — which is exactly why history must not live there. Two behaviours in that
ticket were measured rather than assumed; keep them.

**Note for 05:** the receipt needs a record of what a turn changed, so leave the
turn boundary something this store can describe.
