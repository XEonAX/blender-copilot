# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those
roles to the strings this repo's tracker actually uses.

| Role | String here | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate this issue |
| `needs-info` | `needs-info` | Waiting on reporter for more information |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an AFK agent |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be actioned |

Every string equals its role name. Nothing to translate, and no existing
vocabulary to collide with.

## Where the string goes

This tracker is local markdown, so there are no labels — a role is recorded *in
the issue file itself*, on a `Triage:` line beside `Type:` and `Status:`:

    Type: task
    Status: open
    Triage: ready-for-agent
    Blocked by: none

`Triage:` is deliberately **separate from `Status:`**. `Status:` is the wayfinder
work state — exactly four values (`open`, `claimed`, `resolved`,
`human-required`), machine-read by the frontier scan documented in
`issue-tracker.md`. Triage is a different axis; it must not widen that
vocabulary. Lowercase, one value, nothing else — the same discipline `Status:`
gets.

An issue needs exactly one `Triage:` value once it has been triaged. A
wayfinder ticket that has never been through triage simply has no `Triage:` line,
which is not a missing value.
