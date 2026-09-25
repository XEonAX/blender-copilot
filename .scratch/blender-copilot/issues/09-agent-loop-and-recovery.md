# The agent loop's control flow and failure policy

Type: grilling
Status: open
Blocked by: 06

## Question

What is the loop's state machine, and what does it do when things go wrong? This is where "auto-run arbitrary code with no gate" gets its discipline.

Decide:

1. **Turns and rounds.** The loop is request → tool call → execute → results → request. What caps the rounds per turn, and what stops a runaway loop that keeps calling tools without converging?
2. **Failure feedback.** After a failed `bpy` call, what exactly does the model see — full traceback, a summarised error, or nothing? Does it get to retry automatically, and how many times before the loop stops and hands back to the user?
3. **Partial application.** The model runs code that half-succeeds (three objects created, then an exception). How is that presented, and does the loop try to unwind it or report it and continue?
4. **Asking instead of guessing.** Prior art treats this as the top trust rule: when a request is ambiguous ("make this thicker", "it"), the agent should state the target it resolved before mutating, and ask when several candidates remain. Define the resolution order and the point at which it must ask.
5. **Cancellation semantics.** What Stop does to an in-flight stream and to an in-flight `bpy` execution (which cannot be interrupted mid-statement). Does a cancelled turn leave history consistent?
6. **The loop's own errors.** What happens when the API is unreachable, the key is rejected, or a tool-call payload is malformed — retry, abort the turn, or surface and wait?

Read the answers to *What exactly happens when we exec model code, and what does undo cover?*, *How do we call the API off-thread and stream the reply into the UI?*, and *The three tools' contracts* first — this ticket decides policy over the mechanics they establish.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
