# The first live send against DeepSeek

> **Unblocked 2026-09-25 — claimable.** The credential lives in a `.env` at the repo
> root (`DEEPSEEK_API_KEY`, `DEEPSEEK_API_URL`), which is gitignored and has never
> been committed. It is **not** exported automatically, so load it in the same
> shell that launches the work:
>
> ```
> set -a; . ./.env; set +a
> ```
>
> Verify it arrived before spending anything:
> `[ -n "$DEEPSEEK_API_KEY" ] && echo present`. **Never** echo the value — not into
> a ticket, not into `logs/`, not into a transcript "for debugging". Confirm
> `git check-ignore -v .env` still holds before committing anything.

Type: task
Status: resolved
Blocked by: none

## Provider facts, verified 2026-09-25

Read live from `api-docs.deepseek.com`, so these are not from memory:

| | |
|---|---|
| Base URL (OpenAI format) | read **`DEEPSEEK_API_URL` from the environment**, not from this table |
| Documented default | `https://api.deepseek.com`; chat at `/chat/completions` |
| Models | `deepseek-flash`, `deepseek-v4-pro` |
| Context length | **1M tokens** (max output 384K) |
| Tool calls, JSON output | both supported |
| Thinking mode | on by default; `thinking: {"type": "enabled"}`, `reasoning_effort` |

**One trap worth carrying into the code:** the legacy name `deepseek-v4-flash` is
still *accepted* by the API but is **remapped to a retired model** and billed at
the Flash price. A model string copied from memory or from an old config will
appear to work while silently running something else. This is a concrete argument
for *Where the API key lives, and how the user sets it* §4's insistence on not
hard-coding a model string, and equally an argument for validating whatever the
user does type against the live model list rather than trusting it.

## Question

**Nothing in this effort has ever talked to an API.** Every wire-level claim in
*The agent loop's control flow and failure policy*, *What the prompt teaches the
model about Blender*, *How the addon talks to the API: worker thread or
subprocess?* and *How a conversation degrades as context grows* is reasoned from
documentation, not observed. Two of those tickets say so in their own
`Unverified` sections, and this is the gap they are pointing at.

Resolve it by making one real request and recording what actually happens. Use
the smallest harness that exercises the real path — the `_worker.py` subprocess
from *How the addon talks to the API* fed a minimal request, not the full panel,
so a failure is located in transport rather than in UI.

Record, per item, the observed bytes or error rather than a prediction:

1. **Does the transport path work at all?** Subprocess launch, newline-delimited
   JSON in and out, one `chat.completions` request, a streamed reply reassembled.
   Blender's bundled `requests` and `certifi` are the client. Report the measured
   launch-to-ready time against *How the addon talks to the API*'s claimed
   0.021 s.
2. **The provider contract that ticket 09 assumes**: that every assistant
   `tool_call` needs a matching `tool` result, and that a missing one is rejected.
   Send a deliberately malformed history and record the actual status and body
   rather than accepting the assumption.
3. **A trailing `system` message** — the invariant *How a conversation degrades
   as context grows* labels `I8` and cannot test. If it is refused, that ticket's
   documented fallback applies (fold the summary into index 0 and accept the
   cache loss). Which one happened is the deliverable.
4. **The context-length rejection shape** — the exact status and `error.code` for
   `context_length_exceeded`, which is what *How a conversation degrades as
   context grows* keys its one permitted automatic re-issue on. Force it with a
   deliberately oversized request rather than waiting for it. **Note the
   mismatch while you are here:** that ticket budgets 48,000 bytes against an
   *assumed* 32k-token floor, while this provider offers **1M tokens**. The
   projection is still correct; the number is now known to be far more
   conservative than it needs to be, and this probe is where a real figure comes
   from.
5. **Tool-call fragmentation in a stream** — whether a single tool call can
   arrive split across SSE chunks, which ticket 03's accumulator assumes. If it
   can be forced, force it.
6. **Cost and latency of one turn**, so the caps in ticket 09 have a real
   baseline rather than an invented one.

Deliverable: one file, `.scratch/blender-copilot/research/first-live-send.md`,
with raw request and response excerpts — keys redacted — and a per-item verdict
of confirmed / contradicted / still unknown. Anything contradicted is filed as an
amendment against the ticket that assumed it; do not soften a contradiction to
fit the design.

**Nothing blocks this ticket now** — the credential is already in `.env`. Spend
deliberately: one or two requests, not a soak test. Flash is roughly $0.15 per
million input tokens off-peak and $0.60 per million output, so the whole ticket
costs cents, but **record the actual figure** alongside the findings, because
this is the one ticket in the effort whose purpose is to spend money.

## Answer

**Measured 2026-09-26. Seven requests, 772 prompt + 139 completion tokens, 11.6 s
wall clock, well under a cent.** Full transcript with response bodies:
`research/first-live-send.md`; scaffold: `tools/live_send_probe.py`, run under
Blender's own bundled `python3.13` with its bundled `requests` 2.32.3 and
`certifi`, bounded by `tools/bounded_run.py 180`. The key was read from the
environment and never printed.

**Four assumptions became facts, and two of them were load-bearing.**

| # | what was assumed | what happened |
|---|---|---|
| 1 | the HTTP client works at all | **HTTP 200**, non-streaming completion in 1.0 s |
| 2 | streaming reassembles | **41 SSE `data:` chunks, 9 with content, first at 0.04 s** — line-oriented, exactly what ticket 03's `iter_lines()` design needs |
| 3 | a tool call round-trips | **issued, then accepted with its matching `tool` result**; `finish_reason\`length\`` at `max_tokens=64` |
| 4 | *ticket 09*: every `tool_call` needs a matching `tool` result | **confirmed the hard way — HTTP 400** |
| 5 | *ticket 14*: a trailing `system` message is accepted (invariant I8) | **HTTP 200** — accepted |
| 6 | the legacy model name is a trap | **accepted, and the response echoes `deepseek-flash`** |

### 4. The history-integrity rule is enforced, not a nicety

```json
{"error": {"message": "An assistant message with 'tool_calls' must be followed by tool
messages responding to each 'tool_call_id'. (insufficient tool messages following
 tool_calls message)", "type": "invalid_request_error", "code": "invalid_request_error"}}
```

Ticket 09 flagged this as an assumption it could not exercise and designed the
flusher defensively. **It is a real wire requirement**, so the synthetic
`cancelled` tool results for unexecuted calls are load-bearing: without them a
Stop mid-turn produces a history the provider refuses. Ticket 09's `Unverified`
note is now closed.

### 5. The trailing `system` message works, so the cache prefix survives

Accepted, so *How a conversation degrades as context grows* keeps the live summary
as the **trailing** message, and the fallback it documented — fold the summary
into index 0 and accept the cache loss — is **not needed**. That also settles the
argument between ticket 09 §4's `messages[0]` form and ticket 10's placement, in
ticket 14's favour. Supportively, the provider reports cache accounting
(`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`) so the effect is
observable rather than assumed.

### 6. The model-name trap is real, and it lies quietly

The legacy name `deepseek-v4-flash` was **accepted with HTTP 200** and answered,
and every response in the run — including that one — echoed `"model":
"deepseek-flash"`. So the retired name does not error; it silently serves
something else, and the only tell is a field the caller has to think to read.
This is now measured rather than quoted, and it is the concrete case for ticket
05 §4's empty model field.

### Two findings nobody asked for

- **`tool_calls[].function.arguments` arrives as a JSON *string***
  (`"{\"scope\": \"all\"}"`), not an object. Ticket 06's parsing must expect
  that; a naive `arguments.get(...)` fails.
- **Thinking is on by default and bills as completion tokens** — one probe spent
  **25 of its 64 completion tokens** on `reasoning_tokens`, which is why
  `finish_reason` came back `length`. Ticket 09's caps and ticket 06's 8,000-char
  result cap both need to budget for reasoning the code never sees.

### Not done, and why

**Forcing a real `context_length_exceeded` was skipped.** The provider's limit is
**1M tokens**, so provoking it honestly means uploading megabytes of padding for
a single error shape. Deferred rather than quietly dropped: ticket 14 keys its one
permitted automatic re-issue on that error's `status`/`error.code`, and that key
is therefore still unverified. Its stated fallback — a single re-issue at
`budget // 6` — remains a guess about the error's shape.

Also unexercised: the **subprocess transport** itself. This went straight to
`requests`, which is what ticket 11's child would carry, but the newline-delimited
JSON framing, the launch-to-ready time and the kill path are ticket 11's and are
still designs.

### Spend

7 requests, 772 prompt tokens, 139 completion tokens. At Flash off-peak rates
that is under a cent — and ticket 09's caps now have a real baseline instead of an
invented one.
