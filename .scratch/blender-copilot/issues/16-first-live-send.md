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
Status: open
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

<!-- recorded on resolution; not written at chart time -->
