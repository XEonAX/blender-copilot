# The agent loop's control flow and failure policy

Type: grilling
Status: resolved
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

**RATIFIED 2026-09-25 by the project owner — accepted as written.** This ticket fixes only control flow and failure
policy; it inherits mechanics from [What exactly happens when we exec model
code](02-code-execution-and-undo.md), [How we call the API
off-thread](03-networking-and-threading.md), [The three tools'
contracts](06-tool-contracts.md), [the conversation UX](08-panel-conversation-ux.md),
[transport](11-http-transport-thread-or-subprocess.md) and [the recovery
mechanism](12-recovery-mechanism.md). Prompt wording is [ticket
10](10-system-prompt-and-blender-idioms.md); the capability namespace is ticket 12.

### 0. The spine — a per-tick state machine, one turn in flight

Send is disabled while a turn runs and Stop replaces it (ticket 08 §6), so the loop never
interleaves two turns. States:

- `IDLE` → `REQUESTING` — the child streams one API request; the main thread drains events
  on each timer tick. Ends on `[DONE]`/finish, or a transport error.
- `TOOL_QUEUE` — the assistant message (with `tool_calls`) is committed; pending calls run
  **one per timer tick**, in `index` order. After each, append its `tool` result.
- `FINALIZING` — terminal turn. Capture the post-summary, diff against the pre-summary,
  push the turn's undo step if mutated (ticket 12 §1), emit the receipt, go `IDLE`.

The `bpy.app.timers` callback performs exactly one bounded step per tick: drain events,
**or** execute one tool call, **or** finalize. A tool call blocks the loop for its whole
duration (exec is synchronous on the main thread — ticket 08 §3), so two calls in one tick
could never draw the first one's `running…` row. Rejected: a blocking round-loop inside one
callback (no running state, and Stop is undelivered for the whole turn); N tool calls per
tick (same, plus a long freeze).

### 1. Turns and rounds — caps and runaway

A **turn** = one user message → N **rounds** → a terminal state. A **round** = one
`REQUESTING`. Caps, all checked at the `TOOL_QUEUE → REQUESTING` boundary:

- `MAX_ROUNDS = 8` per turn.
- `MAX_TOOL_CALLS = 24` per turn (bounds a round that returns many parallel calls).
- **Repeat detector:** canonicalize each call to `(tool, sorted-json(args))`; a **third**
  occurrence of the same signature stops the turn.
- **Consecutive-failure stop:** 3 consecutive rounds in which *every* executed call returned
  `ok:false` stops the turn.

On any cap: flush results for unexecuted queued calls (§5), finalize, and append one line
naming the cap (`Stopped after 8 rounds. Send “continue” to keep going.`). Never
auto-continue. Why 8/24: a normal ask is 2–4 rounds (look → act → verify); 8 allows one
self-repair without eating the context window. Rejected: no cap (runaway); 3 rounds (too
few for real multi-step work); a token/$ budget (accounting unverified, and it does not stop
a cheap infinite tool loop).

### 2. Failure feedback

Inherited from ticket 06: the model receives the **full capped traceback** in the error
envelope, `ok:false`, and control. Policy on top:

- A failed tool call **is normal round input**; the model may retry, bounded only by §1.
- The loop retries **nothing** on the model's behalf — not a tool call, and not the request
  that follows it.
- When a cap stops the turn, the last envelope is what the user sees in the error block
  (ticket 08 §4), and the turn is marked incomplete.

### 3. Partial application — report, never unwind

`exec` is not transactional, `bpy` has no transaction, and the `.blend` copy is opt-in and
once per session (ticket 12 §2). Therefore:

- The error envelope already contains the traceback *and* the stdout accumulated before the
  raise — the model can see how far it got. The loop adds nothing.
- The turn's pre/post summary diff (ticket 12 §3) is the report of actual effect. Receipt:
  `⚠ partial · 3 objects created, then error · Ctrl+Z reverts the whole turn`.
- The undo push still fires in `FINALIZING` because the turn mutated, so Ctrl+Z reverts the
  half-applied turn as one step. That is the only recovery and it is the honest one.
- The loop does **not** auto-issue a cleanup call and does not synthesize a “partial
  success” envelope (the tool cannot know what landed). It feeds the diff back as the next
  round's live context so the *model* may choose to finish or clean up.

Rejected: auto-unwind by re-running an inverse (needs a snapshot we do not take, and is
wrong for nondeterministic ops); a `.blend` snapshot per failed call (ticket 12 §2 cost).

### 4. Asking instead of guessing

**Resolution order** for any phrase naming a target:

1. Exact / case-insensitive `bpy.data` name in the user's message.
2. `bpy.context.active_object` (or the relevant active datablock).
3. The selection, if `selection.count == 1`.
4. The selection, if `selection.count > 1` → that set is the candidate set; do not pick one.
5. Anything else → **ask**.

**The loop's mechanical contribution:**

- The per-request live context is `messages[0] = system(base_prompt + "\n\nLive scene:\n" +
  summary)`, recomputed at **every** request. The summary always carries `active:{name,type}`
  and `selection:{count,names}` (ticket 06 §2), so steps 2–4 need no tool call and are at
  most one round stale. The stored conversation stays verbatim wire format (ticket 04); the
  summary is never persisted. Rejected: persisting it (stale within a turn, bloats the file);
  a mandatory `get_scene_info` before every mutation (an extra round trip for data the loop
  already has).
- `purpose` is required on `run_blender_python` (ticket 06), so the resolved target is
  **stated before mutating** and lands in the receipt. A missing `purpose` is already a
  `tool_argument_error` — that is the loop's one hard gate.
- **Ask point = the first mutating tool call of the turn.** When rule 5 applies the model
  must not have mutated yet. A round with text and **zero** `tool_calls` is the ask: the loop
  treats it as terminal and hands back; the user's reply is the next turn. No “ask” tool is
  needed.

Honest limit, stated rather than hidden: the loop cannot judge natural-language ambiguity
and does not try. `purpose: "do it"` still passes the schema. The gate is prompt plus
receipt, not a semantic check; a wrong guess is on screen immediately and one Ctrl+Z away.

### 5. Cancellation semantics

- **Stream:** Stop writes `{"cmd":"cancel"}` to the child (ticket 11). Partial content is
  kept and committed with `[stopped]`; if cancel lands before any content, commit nothing
  (no empty assistant message).
- **Unexecuted queued tool calls:** the assistant `tool_calls` message is already in history,
  so each `tool_call.id` must receive a `tool` result before the next assistant message.
  Flush each as `{"ok": false, "tool": …, "error": {"kind": "cancelled", "message":
  "Cancelled by user"}}`. One shared flusher serves both the Stop and cap-exhaustion paths,
  so history is never left with an orphaned call. (Ticket 04 flagged this as a
  provider-contract assumption; see *Unverified*.)
- **In-flight `run_blender_python`:** cannot be interrupted. Exec is synchronous on the main
  thread and the event loop does not pump while it runs, so the Stop click is not even
  delivered until it returns (ticket 08 §6). Cancel is therefore *pending*: the next tick
  checks it and stops before the next call / next request. Panel shows `running code — cannot
  be interrupted`.
- A cancelled turn that mutated still pushes its undo step and gets a receipt (`stopped
  after partial changes — Ctrl+Z reverts this turn`). No auto-resend; the user resends.

### 6. The loop's own errors

One classification point at the drain/parse boundary. Every class is terminal (error block,
per ticket 08 §4) except malformed tool arguments, which is fed back.

| class | detection | action |
|---|---|---|
| worker unavailable | no `ready` in ~5 s, or spawn error (ticket 11) | persistent panel error, Send disabled, Retry button; **never auto-retry** |
| auth 401/403 | `raise_for_status()` *before* iterating | distinct `AuthError`, one line, turn ends, no retry (ticket 05 §3) |
| `model_not_found` | status + `error.code` | line naming the model; no retry (ticket 05 §4) |
| rate limit 429 | status | line incl. `Retry-After` if present; no auto-retry; user resends |
| timeout / drop / TLS | `requests` exception class | turn ends, partial text kept `[stopped]`, marked incomplete; no auto-retry |
| mid-stream `{"error": …}` | decoded event has an `"error"` key | as above, with the server's message |
| **malformed tool-call JSON, streamed cleanly** | accumulated args fail `json.loads` **and** `finish_reason != "length"` | **do not abort**: append a `tool` result of kind `tool_argument_error` carrying `raw` (≤500 chars) against the id, and let the model fix it; counts toward §1 |
| truncated tool call | `finish_reason == "length"` | terminal: the call is incomplete, not wrong; tell the user the reply hit the limit |
| unknown/empty tool name | not in the three-tool registry | `tool_argument_error` naming it; execute nothing |
| empty reply | no content, no tool calls | terminal note `model returned nothing`; do not loop |
| unparseable body / no `choices` | JSON or shape error on a completed response | terminal transport error |

Rationale: malformed arguments are the model's to fix and cost one cheap round; the others
are not fixable by the model, and re-requesting cannot help — consistent with tickets 03
and 11 (“there should not be an automatic retry for a stream”). One redaction boundary,
`sk-[A-Za-z0-9_-]+ → sk-…` (ticket 05 §2), so no error path can leak a key.

### What this ticket does not decide

Prompt wording (ticket 10); the capability namespace and full-power gate (ticket 12);
transport internals (ticket 11).

### What a human must ratify

1. The caps: 8 rounds, 24 tool calls, stop on a repeated call signature (after 2), stop after
   3 consecutive all-failing rounds.
2. **Report, never unwind** partial application, with the turn-level diff as the receipt and
   the turn-end undo push as the only revert.
3. The resolution order and the ask point (before the first mutating call), including the
   admitted limit that `purpose` is the only mechanical gate.
4. Synthetic `cancelled` tool results for unexecuted calls as the history-integrity rule, and
   no auto-resend after Stop.
5. No automatic retry anywhere; malformed-arguments feedback is a *continuation*, not a
   retry.

### Unverified

- The provider requirement that every assistant `tool_call` have a matching `tool` result is
  a wire contract I cannot exercise: no `OPENAI_API_KEY` is set in this session. The flusher
  is designed defensively, but its necessity is an assumption until the first live send.
- Timers never fire under `blender -b`, so “one tool call per tick” is a design consequence
  of tickets 03/08, not a measured behaviour of this ticket.

## Comments
