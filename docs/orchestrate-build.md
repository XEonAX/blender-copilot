# Orchestrating the build tickets, one at a time

Invocable as `/orchestrate-build` in VS Code chat — the skill at
`.github/skills/orchestrate-build/SKILL.md` dispatches to the prompt below — or
paste the prompt yourself. It makes the chat agent the **orchestrator**: it
selects, launches, verifies and commits, while each ticket is *implemented* by a
separate executor, a subagent by default.

It replaces waves with a queue. The decision map was drained in parallel because
its tickets are files that answer questions; build tickets edit the same handful
of modules, so running two at once is a merge conflict with extra steps.

## Choosing the executor

Set it once, at the top of the run, then keep it. Both modes are wired up:

| | Mode A — subagents | Mode B — pi coding agents |
|---|---|---|
| Launch | one `runSubagent` call per ticket | `bash tools/build-ticket.sh <NN>` |
| Log | none (the result comes back in the chat) | `logs/build-<NN>.log` |
| Blender state | shares your session | clean process, own env |
| Cost | your model, your context | pi's model, separate |
| Skills | you must point it at `~/.agents/skills/*` | loaded via `--skill` |

**Mode A is the default**, and it is what the prompt below asks for: the whole run
stays in one window and you can read the executor's reasoning as it lands. Reach
for **B** on tickets that spend money or touch the network path — pi gets a
genuinely fresh process, and the launcher pre-flights the frontier itself.

## Why it is shaped this way

- **One ticket at a time, and the orchestrator does not implement it.** Splitting
  implementer from verifier is the whole point; a session that writes the code
  and then declares it correct has checked nothing. Mode A still gets that
  isolation, because a subagent's context is not the orchestrator's.
- **`tools/build-ticket.sh` refuses a ticket that is not on the frontier.** It
  checks `Status: open` and that every number on `Blocked by:` is `resolved`.
  Walking the dependency graph by hand is exactly where an orchestrator slips,
  so the check is mechanical rather than remembered.
- **Commit per ticket, not per wave.** Each verified ticket is a rollback point,
  and a rejected one leaves a working tree you can read.
- **No map.** The build tracker has no `map.md` and should not grow one — the
  ticket statuses and the commit log are already the record. Inventing a
  progress board is work that verifies nothing.
- **Screens and delegation are both allowed** — project owner, 2026-09-26.
  Build tickets include `undo` and `bpy.app.timers` work that cannot be measured
  under `blender -b` at all, and several layout bugs on this effort were only
  ever caught by looking at a screenshot. `AGENTS.md` was updated in the same
  pass, so no executor reads a file that forbids its own existence.

## The prompt

---

Orchestrate the build tickets at `/Users/user/Projects/blender.anx.copilot`. You
are the **orchestrator**: you select, launch, verify, commit and report. You do
**not** implement tickets yourself. Stop after **3 tickets** unless told
otherwise — serial runs are expensive and every new ticket depends on the last
one actually working.

Read first: `AGENTS.md`, `docs/agents/issue-tracker.md`,
`.scratch/blender-copilot-build/issues/`, and `docs/ratification.md`.

**Default to Mode A — you are already the orchestrator here.** Delegate each
ticket to a subagent and keep every check for yourself.

- **Mode A — subagents.** One subagent per ticket, one at a time. Call the
  subagent tool with **no `agentName`**: the read-only agent (`Explore`) can read
  but cannot implement, so naming it hands you an executor that cannot write.
- **Mode B — pi.** `bash tools/build-ticket.sh <NN>`, one process at a time.
  Prefer it when the ticket spends money — pi gets a clean process and the
  launcher pre-flights the frontier itself.

Never mix modes inside a ticket. Never run two tickets at once.

**Then loop:**

1. **Select.** The lowest-numbered `Status: open` ticket whose every `Blocked by:`
   is `resolved`. Read it in full, and read the decision tickets it names by
   title (`grep -rl "<title>" .scratch/blender-copilot/issues/`).
2. **Launch.** Mode A: `runSubagent` with the ticket brief below verbatim,
   including the ticket's filename. Mode B: `bash tools/build-ticket.sh --dry-run
   <NN>` to see the brief, then `bash tools/build-ticket.sh <NN>`. Run it as a
   single command and expect a long wait. If the terminal moves it to the
   background, wait for the completion notice; do not poll.
3. **Verify — you, not the executor.** All of:
   - `git diff --stat` — did it change what the ticket is about, and *only* that?
   - `python3 tests/test_conversation.py` — must exit 0.
   - the panel draw smoke, bounded and gated on its token:
     `python3 tools/bounded_run.py 60 -- /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python tools/panel_draw_smoke.py`
     then grep the output for `SMOKE OK`. Blender exits **0** even when a
     `--python` script raises, so the token is the verdict and the status is not.
   - **Pick two of the ticket's own evidence claims and re-run them yourself.**
     A claim with no command behind it is not evidence, and an executor that
     pasted a command it never ran is the failure this step exists to catch.
   - read the ticket's `## Answer` and confirm the unticked boxes are unticked
     *and explained*.
4. **Commit or reject.**
   - Pass → commit that ticket alone, message naming the ticket and what landed.
     Then next ticket.
   - Fail → do not commit. Read the executor's log, reset the ticket to
     `Status: open`, append a `## Verification failed` section quoting the exact
     command and its output, and retry **once** with that note included. Two
     failures on the same ticket → `Status: human-required`, a `DO NOT CLAIM`
     banner, and move on.

**Stale claims.** A ticket left at `claimed` whose log shows a crash is stale:
reset it to `open` and retry once. A `claimed` ticket whose process is still
running is not stale — read the log before touching it.

**Rails, unchanged from `AGENTS.md`:**

- One ticket per session. Never edit another session's ticket.
- Never modify anything under `/Users/user/Projects/blender`.
- Never print, echo or commit `DEEPSEEK_API_KEY`. Load `.env` with
  `set -a; . ./.env; set +a` in the shell that launches the work.
- Chain checks with `&&`, never `;`. A `;` runs the next step whether or not the
  last one failed, and a broken build gets committed while the traceback scrolls
  past.
- Bound every probe with `tools/bounded_run.py`. Never run a hang case in the
  foreground. macOS has no `timeout(1)`.
- **Screens are allowed, screenshots included.** An executor may run Blender with
  a window and read back what it drew whenever looking is the honest way to
  check. The mechanics still apply, because they are about hangs rather than
  screens: background it, bound it with `tools/bounded_run.py`, have the script
  quit Blender itself, write results to a file.
- **Delegation is allowed** (`AGENTS.md`, 2026-09-26). An executor may hand an
  independent probe to a subagent of its own; what it may not do is hand off the
  ticket itself — it owns that status line and that answer.

**Report and stop:**

- each ticket: landed / rejected and why, with the commit hash
- every claim you could not reproduce
- the frontier now, and what is blocked on what
- anything now `human-required`, and exactly what a human must do

---

## The ticket brief

Both modes send this; `tools/build-ticket.sh` prints it under `--dry-run`. It
claims, reads, implements, gates, ticks, answers, resolves — and touches nothing
else.

## Verifying an executor yourself

Mode A's record is the chat itself; Mode B writes a log you can reread. Both are
checked the same way — by running the commands, not by reading the prose:

```sh
tail -40 logs/build-01.log                        # Mode B's transcript
grep -E '^\*\*(Status|Triage|Blocked by):' .scratch/blender-copilot-build/issues/*.md
git diff --stat                                   # what it actually changed
git log --oneline -3                              # what the orchestrator committed
```

To resume a pi executor's session interactively instead of reading a log:

```sh
pi --resume            # pick the build-<NN> session
```

## If a ticket fails twice

The `human-required` ticket is the honest outcome, not a failure of the run. It
means the ticket needs something an agent does not have — a judgement call, a
credential, or four hands on a GUI. Read its `DO NOT CLAIM` banner; the body says
what a human must do.
