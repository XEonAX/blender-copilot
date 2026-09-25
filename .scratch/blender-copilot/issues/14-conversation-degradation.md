# How a conversation degrades as context grows

Type: grilling
Status: resolved
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

**PROVISIONAL — no human present.** Depends on [Where chat history
lives](04-chat-history-storage.md) §4 (store cap, wire-format messages), [The
agent loop's control flow and failure policy](09-agent-loop-and-recovery.md)
(rounds, one call per tick, no-retry rule), [The three tools'
contracts](06-tool-contracts.md) (8,000-char results), [What the prompt teaches
the model](10-system-prompt-and-blender-idioms.md) (live summary) and [the
conversation UX](08-panel-conversation-ux.md) (paging).

**One-line verdict.** The store is the user's record; **degradation is a
deterministic request-time projection that is never written back**. No
model-authored summary. Evict by *cost × reconstructibility*, not age: elide
old tool results first, then drop call/result pairs, then drop whole turns.
Measure in **UTF-8 bytes of the wire JSON**, budget **48,000 bytes** for the
prunable history, assume **3 bytes/token**, and make the API's
`context_length_exceeded` the one permitted automatic re-issue.

### 0. Corrections to prior tickets' premises

- **Ticket 09 §4 says the live context is `messages[0] = system(base + live
  scene)`, recomputed per request; ticket 10 says a second `system` message
  after the stable prefix.** Ticket 09's form destroys prefix caching on every
  request *and* contradicts ticket 10's explicit placement. This ticket fixes
  the position: the live summary is the **trailing message** of every request
  (the invariant this ticket's question names), the stable base prompt stays at
  index 0 untouched, and the summary is recomputed before **every** request — so
  `captured` in ticket 10's field list names a round, not `turn_start`. Ticket
  09 §4's `messages[0]` notation is superseded.
- **Ticket 04 §4 prunes whole turns from the front and does not say how a later
  reader knows.** The store must describe its own gaps: ticket 04's file gains a
  `meta` block (§5). This ticket also adds the self-repair rule that makes a
  front-pruned file safe to project (invariant I6).
- **Ticket 09's "no automatic retry anywhere" meets its one honest exception
  here** — a *deterministic down-shift* on `context_length_exceeded`, not a blind
  retry (§1).

### 1. What is sent

**The projection.** `context.py` builds the request from the stored conversation
on every request:

```
[ system(base prompt, frozen) ]
[ head marker, only if degraded ]
[ surviving stored messages, in store order, some edited in place ]
[ system(live scene summary + trim line) ]   <- always last, never stored
```

`plan(messages, budget_bytes, current_turn_start) -> (sent, report)`; it imports
no `bpy` and is pure, so it is testable on plain CPython exactly like
`conversation.py`.

**Measurement: UTF-8 bytes of the wire JSON.** There is no tokeniser in the
bundle — *verified* just now against the installed build's own interpreter
(`/Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13`):
`tiktoken`, `tokenizers`, `transformers`, `sentencepiece` are all absent; only
stdlib `json` is there. So per message the cost is
`len(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())`.
Bytes, not chars, because UTF-8 byte length is the most script-uniform proxy
(*verified*: an 11-char Japanese string is 11 chars but 33 bytes). The planning
divisor is **3 bytes/token**: ASCII English ≈ 4.2, code/JSON ≈ 3, CJK ≈ 3. It is
an overestimate for prose (evicts slightly early = safe) and near-exact for the
scripts that break a chars-per-token rule.

**Budget.** `HISTORY_BUDGET_BYTES = 48_000` (~16k tokens) applies **only to the
prunable history**; `system(base)`, the live summary and the whole in-flight turn
are excluded because they are never pruned. Sizing, from an assumed 32k-token
floor: 96 KB window − 4 KB prompt − ≤1.5 KB summary − 12 KB reserved output
(4,096 `max_tokens`) − 48 KB history = ~30 KB left for the current turn. The
assumed floor is a choice, not a lookup — the model name is free text (ticket 05)
and no window is introspectable. A user with a large model can buy back memory
via an addon preference `context_history_kib` (8–512, default 48); nothing else
reads it.

**Error cost, stated.** A wrong estimate costs one of two ways: too high → the
request is rejected and the turn ends; too low → earlier amnesia than necessary.
The first is covered by the single down-shift: on HTTP 400 with
`error.code == "context_length_exceeded"` **before any content has streamed**,
the loop re-projects at `budget // 6` and re-issues the *same* request once. A
second such failure is terminal (ticket 09's error block). This is not a retry
of a failed request — the request is provably too large and the change is
deterministic — and it is the only automatic re-issue in the design.

**Pre-flight warning.** If the in-flight turn alone (user message → last tool
result, verbatim) exceeds `2 × budget`, the panel warns before sending; the turn
is still sent, because dropping its own pending calls would orphan them.

### 2. What degrades first — and why it is not oldest-first

| step | action | scope | order within step |
|---|---|---|---|
| 1 | **elide tool results** — keep the `tool` message and its `tool_call_id`; replace `content` with `{"ok":<ok>,"tool":…,"elided":true,"note":"result elided to fit the context window; re-call the tool if you need it"}` | settled turns | **largest bytes first**, then oldest |
| 2 | **drop tool call/result pairs** — remove the `tool` message(s) *and* the matching entry from the parent assistant's `tool_calls`; keep the assistant's prose | settled turns | oldest turn first, oldest call first |
| 3 | **drop whole settled turns** (user + assistant + remnants) | oldest first, never below `MIN_KEEP_TURNS = 2` | oldest first |
| 4 | **amnesia floor** — drop all settled turns; send base + current turn + summary | — | — |

Step 1 only elides a result whose bytes exceed 512 (a stub that saves nothing is
pure loss). Step 1 keeps `run_blender_python`'s `purpose`, which lives in the
assistant message's tool-call **arguments**, not the result — so after elision the
model still sees *what each call was for*, only not its output.

The order is **cost × reconstructibility**, not age:

1. **Age is the wrong proxy.** A three-turn-old 8,000-byte scene dump is
expensive and re-derivable; a twenty-turn-old one-line user constraint ("use
metres", "don't touch the lights") is cheap and **not** re-derivable. Oldest-first
evicts the second while keeping the first whenever the bulk is newer — exactly
backwards.
2. **Old tool output is not merely stale, it is actively false.** Tool results
describe a scene that has since changed, and the live summary supersedes them. A
stale 8 KB enumeration is a *correctness* hazard, so eliding it improves the
request as well as shrinking it. The tool is still there and the model may
re-call.
3. **Oldest-first couples cheap asks to expensive bulk**, because a turn's head is
its user message and its tail is the tool noise. Degrading by kind decouples
them: intents survive longest (until step 3), machine output goes first.
4. **Anaphora still breaks at step 3**, which is why step 3 is last and floored at
two turns, and why the trim is announced (§5) rather than silent.

Rejected: dropping oldest-first wholesale (above); a token/$ budget (no
tokeniser; accounting unverified; does not stop a cheap loop); a
`max_tokens`-style hard error with no projection (the current behaviour, which is
the bug this ticket exists to fix); summarising tool results back to the model
(that is §3's rejection).

Never degraded: the current turn's messages (invariant I4), the base prompt, the
live summary.

### 3. Summarisation — none. Drop, mark the gap, rely on summary + re-reads.

No model-authored summary is written, at any point.

- **Nothing in the store is summarised** because nothing in the store is
degraded: the projection is ephemeral. The stored file stays byte-for-byte wire
format (ticket 04), so there is no place for a summary to live and no lossy
second schema.
- **The live summary already is the summary.** It is regenerated per request and
is *fresher* than any narrative summary could be; it covers exactly the
reconstructible facts (scene, selection, active, mode, counts).
- **A model-authored summary fails at the worst moment.** It requires a second
API call while the context is already full — the exact state you cannot afford
one in — and it can itself fail, at which point the turn dies with no fallback.
- **It would become the only record and then rot.** Ticket 09 makes stale
references a top trust rule; a stored narrative that outlives the scene it
describes is that failure with a delay fuse.
- **If a gap must be known, say it in one line, not one paragraph.** The head
marker is mechanical: `[12 earlier turns (34 messages) elided to fit the context
window; the live scene summary is current, and tools can be re-called.]`

**How a later reader knows a gap is a gap.** Two distinct gaps, both recorded:
*projection* gaps are recomputable (the store is intact — re-run `plan()`), and
*retention* gaps are recorded in `meta.retention` with a count and a cutoff
timestamp (§5). No later reader ever has to infer a gap from silence.

### 4. Never-break invariants

- **I1 no orphan results** — every `tool` message's `tool_call_id` has a parent
  assistant `tool_calls` entry present *earlier* in the same projection.
- **I2 no dangling calls** — every `tool_calls` entry has a `tool` result later
  in the projection, **except** calls made by the in-flight turn that have not
  executed yet.
- **I3 no empty assistant message** — after stripping `tool_calls`, an assistant
  message with empty `content` and no remaining calls is removed.
- **I4 the current turn is byte-identical** to the store: from its user message
  through its last tool result.
- **I5 order preserved** — the projection is a subsequence of the store with
  in-place edits; messages are never reordered, merged, or duplicated.
- **I6 the projection is self-repairing** — leading `tool`/orphaned fragments left
  by a hand-edited file or an interrupted retention prune are dropped before
  anything else.
- **I7 ids unique** — each `tool_call_id` appears at most once and each `id` at
  most once.
- **I8 the live summary is last** — exactly one trailing `system` message,
  recomputed per request, never persisted, never counted against the history
  budget.
- **I9 budget** — the byte total of the *prunable* portion is ≤ the budget after
  steps 1–3, unless step 4 was reached.
- **I10 idempotence and monotonicity** — `plan(plan(x)) == plan(x)`; a larger
  budget never sends fewer stored messages.

**How the pruner is proved.** `tests/test_context.py`, plain CPython, no
`bpy`:

- A **validator written independently of `plan`** (`validate_projection`) checks
  I1–I5, I7 against the store; the budget checker checks I9; the idempotence and
  monotonicity checks cover I10; a byte-identity check covers I4.
- **Seeded fuzz:** 300 generated conversations (0–4 tool calls/turn, results
  0–8,000 bytes, ~10% `ok:false`, prose sometimes present or absent) × 12 budgets,
  every projection run through the validator. A counterexample is a failing test,
  not an argument.
- **Named adversarial fixtures:** a store beginning with an orphan `tool`
  message; an assistant with `tool_calls` and empty `content`; a result exactly at
  the 512-byte elision threshold; a current turn alone over budget; two consecutive
  rounds whose calls have equal sizes (deterministic tie-break); step 3 landing
  exactly on `MIN_KEEP_TURNS`.
- **Panel rendering:** `tools/panel_draw_smoke.py` gains the new note kind so the
  trim notice is drawn expanded and collapsed for every layout.

### 5. Observability — never silent

- **New message kind `KIND_NOTE`** (an additive amendment to ticket 08's set,
  like ticket 10's amendment to ticket 06). One note is emitted per turn, at the
  moment history first differs from the store, never per round:
  `✂ context trimmed — model saw 3 tool results elided, 2 turns dropped; your
  history is kept`. Expanding it lists which turns went and why.
- **A persistent header chip `✂ trimmed`** whenever `plan(store).trimmed` is
  true. It is **derived at load and after each turn, never read from a stored
  flag**, so it cannot drift from reality.
- **Storage for the record** — ticket 04's conversation file gains a `meta`
  object beside `messages` (wire format untouched):
  ```json
  {"schema": 2, "messages": [...],
   "meta": {
     "retention": {"turns_dropped": 4, "dropped_through": "2026-09-25T10:00:00Z",
                   "reason": "cap_200_messages"},
     "context_trim": {"first_trim_at": "...", "turns_elided_total": 12,
                      "last_trim": {"turns": 2, "messages": 5, "tool_results_elided": 3}}
   }}
  ```
  `meta` is informational (chip history, bug reports), never a second source of
  truth for the projection.
- **Retention is announced too** — where the store itself was pruned (ticket 04
  §4), the oldest reachable page opens with `⋯ 4 earlier turns were removed by the
  history cap`, fed by `meta.retention`. A store that prunes silently is the same
  trust bug one level down.
- No new `Text` datablock; the full stored transcript remains the escape hatch
  (ticket 08), and the note is in-panel and expandable.

### 6. Interaction with paging — confirmed

- **Paging operates on `session.messages` only**: whole messages packed from the
  newest end, page indices are **store** indices (ticket 08). `plan()` is never
  consulted by the pager and the projection is never paged.
- **The panel shows the user's record; the model sees the degraded view.** A
  conversation may be far longer than the model's memory with nothing deleted
  from the file — that asymmetry is the design, not a bug.
- **The difference is visible**: the header chip says trimming is in effect, the
  per-turn note says when it changed and by how much, and expanding the note says
  which turns went. A silently forgetful agent would be a trust bug; here the
  forgetfulness is on screen and the record behind it is intact.

### Unverified

- No API key in this session, so **nothing wire-level is exercised**: that
  OpenAI-compatible servers accept a **trailing `system` message** (I8), and the
  exact status/`error.code` shape of a context-length rejection. **Fallback if the
  trailing message is refused:** fold the summary into index 0 (ticket 09's form)
  and accept the cache loss — I3/I8 are the only invariants that change.
- The 3-bytes/token divisor is a planning constant, not measured against any real
tokeniser (none is installable from the bundle).

### What a human must ratify

1. **No model-authored summary** — drop + one-line marker + live summary +
   re-reads is the whole mechanism.
2. **The eviction order and its rationale** (tool results → call/result pairs →
   whole turns; largest-first by cost × reconstructibility, not oldest-first),
   with `MIN_KEEP_TURNS = 2`.
3. **48,000 UTF-8 bytes, 3 bytes/token, the 32k-token assumed floor, and the one
   exception to "no automatic retry"**: a single `context_length_exceeded`
   re-issue at `budget // 6`.
4. **Degradation is a projection and never written back** — the store stays wire
   format and lossless except for ticket 04's retention cap.
5. **The additive amendments**: `KIND_NOTE` in ticket 08's kinds, the derived
   header chip, `meta.retention` / `meta.context_trim` in ticket 04's file, and
   the live summary as the **trailing** message (superseding ticket 09 §4's
   `messages[0]` form and fixing ticket 10's summary position as recomputed per
   request).

## Comments
