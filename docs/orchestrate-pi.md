# Orchestrating pi over the wayfinder map

Copy the prompt below into VS Code chat. It makes the chat agent the
**orchestrator** — it launches pi instances, verifies them, sweeps the map and
reports — while each ticket is worked by its own pi process.

## Why it is shaped this way

- **One pi process per ticket.** Wayfinder resolves one ticket per session, and
  a ticket is a file, so parallel instances cannot collide on tickets.
- **Instances never touch `map.md`.** The map is shared; concurrent writers
  would clobber it. The orchestrator sweeps the map once, serially, at the end.
- **The rules live in the repo, not the prompt.** `AGENTS.md` is loaded by pi in
  every mode, including print mode and regardless of project trust, so every
  instance inherits the protocol, the house facts and the provisional-decision
  rule without the prompt re-stating them.
- **Waves, not a flood.** The ticket dependency graph decides what may run
  together: design tickets in parallel, the code ticket alone, newly-unblocked
  tickets last.
- **A canary first.** One instance before a fan-out, because an orchestration
  that silently does nothing is worse than one that fails loudly.

## The prompt

---

Orchestrate `pi` instances to drain the wayfinder map at
`/Users/user/Projects/blender.anx.copilot`. You are the **orchestrator**: you
launch, verify, sweep and report. You do **not** resolve tickets yourself.

Read `AGENTS.md` and `.scratch/blender-copilot/map.md` first.

Each ticket is worked by its own non-interactive pi process through
`tools/wayfinder-wave.sh`, which claims, works and resolves that ticket in its
own log.

**Wave 0 — canary (one instance).**
`bash tools/wayfinder-wave.sh --dry-run 04`, check the command looks right, then
`bash tools/wayfinder-wave.sh 04`.
From `logs/04.log` and the ticket file, verify it: claimed *before* working,
actually resolved, and wrote a real answer rather than a summary. If it did not,
**stop and report the log** — do not fan out.

**Wave 1 — design tickets, in parallel.**
`bash tools/wayfinder-wave.sh 05 06 11 12`

**Wave 2 — the code ticket, alone.**
`bash tools/wayfinder-wave.sh 08`

**Wave 3 — newly unblocked once `06` closes.**
`bash tools/wayfinder-wave.sh 09 10`

Run each wave as a **single command** and expect it to take a long while. If the
terminal moves it to the background, wait for the completion notice; do not poll.

Between waves: if a ticket is still `Status: claimed` but its log shows a crash,
reset it to `open` and retry it once. Then `git add -A && git commit`.

**Then sweep — you do this, serially:**

1. For each newly resolved ticket, append one line to `map.md` under
   Decisions so far: the ticket's **name** as the link text plus a one-line gist.
2. Graduate fog: move anything now-specifiable out of *Not yet specified* into
   new tickets (create first, then wire the `Blocked by:` lines). Cap it at 3
   new tickets; if more than 3 would graduate, leave them in *Not yet specified*
   and say so.
3. If a resolution overturned a standing decision in the map's Notes, correct
   the Notes rather than leaving a stale premise.
4. Commit.

**Then report and stop:**

- the frontier now, and what is blocked on what
- every PROVISIONAL decision, and exactly what a human must ratify
- anything an instance failed to verify, or claimed without evidence

Rails: do not resolve tickets yourself; do not run a fourth wave; never launch a
GUI application or take a screenshot; never touch `/Users/user/Projects/blender`.

If you would rather not spend on a full run, stop after Wave 0 and Wave 1 and
report — that covers every decision ticket that is currently unblocked.

---

## After it finishes

The map's Decisions-so-far will hold several entries marked provisional. That is
the point: they are recorded as *agent decisions awaiting signature*, not as
settled facts. Ratify, amend or reject them, then rerun the same prompt to take
the next wave.

## Verifying an instance yourself

```sh
tail -40 logs/06.log                        # what it said
grep -E '^(Type|Status|Blocked by):' .scratch/blender-copilot/issues/*.md
git diff --stat                             # what it actually changed
```

To resume an instance's session interactively instead of reading a log:

```sh
pi --resume            # pick the wf-<id> session
```
