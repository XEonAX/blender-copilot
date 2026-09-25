# Agent instructions

## What this repo is

A Blender extension that puts a Copilot-style chat panel **inside Blender**.
Development happens here.

`/Users/user/Projects/blender` is a **read-only reference clone**. Never modify
anything under it: no edits, no builds, no `git` commands.

Target: the **installed** Blender **5.2.2** at `/Applications/Blender.app`
(Python 3.13.13). The clone is 5.3.0-alpha and unbuilt — treat any API claim
that turns on a 5.2-vs-5.3 difference with suspicion, and verify against the
installed build.

## Where the plan lives

The work is charted as a wayfinder map:

| | |
|---|---|
| Map | `.scratch/blender-copilot/map.md` — read this **first** |
| Tickets | `.scratch/blender-copilot/issues/NN-*.md` |
| Research | `.scratch/blender-copilot/research/*.md` |
| Tracker conventions | `docs/agents/issue-tracker.md` |

## Working a ticket

1. **Claim before work.** Edit the ticket file and set `Status: claimed`. That
   edit *is* the claim — other sessions may be running right now and skip a
   claimed ticket.
2. **One ticket per session.** Never resolve two.
3. Append the answer under the ticket's `## Answer` heading, then set
   `Status: resolved`.
4. **Never edit `map.md`, and never edit another session's ticket file.** The
   orchestrator updates the map in one serial pass at the end, which is what
   stops concurrent writers clobbering it.
5. **No git commands that write.** `git status`, `git log` and `git diff` are
   fine and sometimes the honest way to check your own work; `add`, `commit`,
   `checkout`, `stash`, `reset` and friends are the orchestrator's, at wave
   boundaries. Nothing else in the repo is off limits to a read.
6. **If your ticket cannot be resolved without a human**, set
   `Status: human-required`, add a `DO NOT CLAIM` banner above the heading, and
   say in the body exactly what a human would have to do. Do not guess on the
   human's behalf and do not file it as `resolved`.
7. Do not delegate to subagents. Do the work yourself.

## Provisional decisions

No human is present during orchestrated runs. For `Type: grilling` tickets you
must still decide, but record the decision as **PROVISIONAL**:

- the recommendation,
- the evidence behind it,
- the alternatives rejected and why,
- and a final line naming **exactly what a human must ratify**.

Never present a provisional decision as settled. A map full of agent
assumptions wearing a human's signature is worse than an unresolved ticket.

## Verify, don't assert

- Prefer running something over reading about it. Say **how** you verified.
- Cite file paths with line numbers, or URLs, for factual claims.
- State plainly what you could not verify.
- **Never launch a GUI application and never take screenshots.** Hand visual
  judgement to the human and say so.

## House facts — established, do not re-derive

- Blender's Python bundles `requests` and `certifi`. It does **not** bundle
  `httpx`, `openai`, `anthropic` or `keyring`. HTTPS to `api.openai.com` works
  from it.
- A panel cannot register a new space type, cannot scroll, and has **no rich
  text**. `UILayout.textbox()` exists in 5.2.2 and is the only multi-line input.
  Repaint by tagging a region redraw from a `bpy.app.timers` callback.
- Operators called from Python **never push undo**. Undo reaches only local
  `bpy.data`, and only after an explicit push — which belongs at the *end* of
  the unit being reverted.
- `bpy.app.online_access` (and `--offline-mode`) do **not** block Python
  sockets. The network permission is a declaration, not a sandbox.
- Probing RNA: `hasattr` on an RNA type is an invalid test. Use
  `bpy.types.X.bl_rna.functions`.
- `blender_copilot/` is a working prototype: the manifest validates and it
  installs and enables as `bl_ext.user_default.blender_copilot`.

## Credentials

A `.env` at the repo root holds **`DEEPSEEK_API_KEY`** and
**`DEEPSEEK_API_URL`**. It is listed in `.gitignore` and has never been committed
— keep it that way, and check (`git check-ignore -v .env`) before you trust that.

- Load it with `set -a; . ./.env; set +a` in the shell that launches the work, so
  a Blender subprocess inherits the values. A `.env` is **not** loaded
  automatically — nothing in Blender or in this repo reads it for you.
- **Never** print, echo, log or commit the key. Not into a ticket, not into a
  `logs/` file, not into a transcript "for debugging". Refer to it by name.
- Read `DEEPSEEK_API_URL` rather than hard-coding a base URL, and never hard-code
  a model string from memory — the provider's own docs show a legacy name that is
  still accepted but silently remapped to a retired model.
- Ticket **16** (*The first live send against DeepSeek*) is the only place a real
  request should be made, and it is the one ticket whose whole purpose is to
  spend a few cents.

## Layout

```
blender_copilot/          the Blender extension (the package)
tests/test_conversation.py   runs on plain CPython
tools/undo_probe.py       manual GUI probe
tools/wayfinder-wave.sh   launches parallel pi instances, one per ticket
.scratch/blender-copilot/ the wayfinder map, tickets and research
dist/                     built packages (gitignored)
```
