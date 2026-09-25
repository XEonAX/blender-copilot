# How a conversation degrades as context grows

Type: grilling
Status: open
Blocked by: none

## Question

*Where chat history lives* fixed the store: JSON under the per-extension user
dir, capped at 200 messages or 1 MiB, pruning whole turns from the front, never
orphaning a `tool` result from its parent `tool_calls`. It explicitly left
**compaction-degradation policy** to this ticket.

So the store is bounded; the *request* is not. Nothing yet decides what the model
sees as a conversation outgrows the context window, and the slice now has the
pieces that determine the answer: the live summary is injected per turn
(*What the prompt teaches the model about Blender*), tool results carry 8,000-char
caps and `truncated` flags (*The three tools' contracts*), and the loop can cap
rounds but not history (*The agent loop's control flow and failure policy*).

Decide:

1. **What is sent.** Verbatim history up to what budget, measured how — chars,
   tokens, message count? There is no tokeniser in the bundle: say what stands in
   for one, and what its error costs.
2. **What degrades first.** Old tool output is the obvious first casualty (large,
   verifiable-again, and the model can re-call the tool); then old tool *calls*;
   then old turns. Fix the order and say why it is not "oldest first".
3. **Summarisation.** Whether a model-authored summary replaces dropped turns at
   all, or whether the loop simply drops and relies on the live summary plus
   re-reads. If a summary is written, who writes it, when, where it is stored
   (it is not wire format, so ticket 04's file needs a place for it), and how a
   later reader knows a gap is a gap.
4. **The never-break invariants.** Which pairs/couplings survive every
   degradation step — `tool` result with its `tool_calls`, an assistant message
   with its tool calls, the live summary's position at the end — and how the
   pruner is tested to prove it.
5. **Observability.** What the panel tells the user when history was trimmed:
   silent, a marker in the transcript, or a persistent note. A silently
   forgetful agent is a trust bug.
6. **Interaction with paging.** *How a conversation is laid out and controlled*
   pages whole messages from the newest end over the *stored* conversation; the
   request may be a subset of that. Confirm the panel shows the stored history
   (the user's record) while the model sees the degraded one, and that the
   difference is visible.

Read *Where chat history lives* §4 and *The agent loop's control flow and failure
policy* §4 first.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
