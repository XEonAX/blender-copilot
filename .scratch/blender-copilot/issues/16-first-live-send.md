# The first live send against a real provider

> **DO NOT CLAIM. This ticket requires a credential no session has.** It needs a
> real `OPENAI_API_KEY` (or an OpenAI-compatible endpoint plus its key) to be
> present in the environment. It is `human-required` for that reason and for no
> other: an agent with the key could resolve it. If a key is set later, flip this
> to `Status: open` rather than filing a new ticket.

Type: task
Status: human-required
Blocked by: none

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
   deliberately oversized request rather than waiting for it.
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

**What a human must do:** put a key in the environment, or decide the effort
proceeds without ever having made a real request — in which case say so in the
map, because "the vertical slice works" would then be a claim about untested
code.

## Answer

<!-- recorded on resolution; not written at chart time -->
