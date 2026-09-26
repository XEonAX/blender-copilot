---
name: orchestrate-build
description: "Orchestrate this repo's build tickets one at a time: pick the frontier ticket, delegate implementation to a subagent, verify it yourself, then commit or reject. Use when asked to work the build tickets, drain the build queue, run the orchestrator, or continue the implementation after the design tickets."
argument-hint: "optional: ticket cap, e.g. 3, or a ticket number to start from"
disable-model-invocation: true
---

# Orchestrate the build tickets

The runnable prompt lives in the repo, next to the reasoning behind it:

- [docs/orchestrate-build.md](../../../docs/orchestrate-build.md)
- ticket sets: `.scratch/blender-copilot-build/issues/` (build), `.scratch/blender-copilot/issues/` (ratified decisions)

## Do this

1. Read `docs/orchestrate-build.md` in full.
2. Execute the prompt under its **## The prompt** heading verbatim.

Do not summarise that prompt back to the user, paraphrase its steps, or invent a
lighter version of the loop. Its shape is the point: one ticket at a time, you do
not implement, and you re-run the evidence before you commit.

The argument, if any, is the ticket cap or a starting ticket — for example
`/orchestrate-build 4` for four tickets, or `/orchestrate-build from 05`.
