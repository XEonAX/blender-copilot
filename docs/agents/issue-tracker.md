# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`, never a
  single combined tickets file
- Triage state is recorded as a `Triage:` line near the top of each issue file,
  separate from the wayfinder `Status:` vocabulary (see `triage-labels.md`)
- Comments and conversation history append to the bottom of the file under a
  `## Comments` heading

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if
needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or
the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` (the Destination / Notes /
  Decisions-so-far / Not-yet-specified / Out-of-scope body).
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`,
  with the question in the body. A `Type:` line records the ticket type
  (`research`/`prototype`/`grilling`/`task`); a `Status:` line records one of
  the four states below.
- **Status vocabulary**: exactly these four values, lowercase, nothing else.
  - `open` — not started.
  - `claimed` — a session is working it now. The claim is the edit that writes
    this value, and it must be written **before any other work**.
  - `resolved` — the answer is appended under `## Answer`. A `resolved` ticket
    is never reopened; if the answer turns out wrong, file a new ticket that
    says so and names the one it supersedes.
  - `human-required` — resolvable only by a person, not by an agent, because
    the work needs something a session does not have: a credential, a physical
    device, or a judgement call that has not been delegated. **Never**
    claimable, by protocol rather than by convention, so a frontier scan
    cannot pick it up by mistake.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked
  when every file it lists is `resolved`. Use `Blocked by: none` when nothing
  blocks it. A ticket parked at `human-required` is not on the frontier at all,
  and blocking on it would stall the effort, so do not list it in `Blocked by:`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files whose `Status:` is
  `open` and whose `Blocked by:` files are all `resolved`; first by number wins.
  `claimed` and `human-required` are both excluded.
- **Claim**: set `Status: claimed` and save **before any work**.
- **Resolve**: append the answer under an `## Answer` heading, set
  `Status: resolved`, then append a context pointer (gist + link) to the map's
  Decisions-so-far in `map.md`.

## Concurrency

Parallel sessions are expected. Each ticket is its own file, so distinct
tickets can be worked at once — but `map.md` is shared:

- Ticket sessions **never** write to `map.md`. They write only their own ticket.
- The orchestrator updates `map.md` in a single serial pass after the wave, so
  concurrent writers cannot clobber it.
- A `claimed` ticket whose process died is a stale claim: reset it to `open`
  before retrying, or it leaves the frontier permanently. A `claimed` ticket
  whose process is still running is **not** stale, so read the log before
  resetting it.
- A ticket that turns out to need a human mid-flight is set to `human-required`
  and a `DO NOT CLAIM` banner is added above its heading, because the status is
  machine-read and the banner is read by whoever is tempted.

## Current effort

Two directories, deliberately separate because their tickets answer different
questions and use different vocabularies:

- **`.scratch/blender-copilot/`** — the *decision* map: a Copilot-style chat panel
  that runs inside Blender. 18/18 resolved. Uses the `Status:` vocabulary above
  with `Blocked by:` edges, and every decision in it was ratified by the project
  owner (`docs/ratification.md`). Read `map.md` first.
- **`.scratch/blender-copilot-build/`** — the *build* tickets that implement that
  ratified design, numbered from `01` in dependency order. These are not decisions
  and must not be mistaken for them: they carry `Triage: ready-for-agent`, and a
  finished one means "code exists", never "a design was settled". Work the
  frontier — any ticket whose blockers are done.

  One carve-out from *a resolved ticket is never reopened*, and it applies only
  here: a **build** ticket may be returned to `open` when the orchestrator's own
  verification fails, with the exact failing command and its output appended under
  a `## Verification failed` heading. A build ticket's `resolved` is a claim about
  code, and code can be re-run — where a decision's `resolved` is a claim about an
  argument, and a second session contradicting it in place would destroy the very
  record the decision exists to keep. The carve-out is for the orchestrator
  correcting its own run, not for a later session disagreeing: a committed ticket
  that turns out wrong gets a new ticket that names it.
