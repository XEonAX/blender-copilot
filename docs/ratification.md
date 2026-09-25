# Ratification worksheet

Every decision below was made **by an agent, with no human in the loop**, because
orchestrated runs have no human present. Each is recorded in its ticket as
`PROVISIONAL`. Nothing here is settled. This file exists so that ratifying them
costs minutes rather than a re-read of eight tickets.

**How to use it.** Ratify per numbered item — "accept 04, 05.1; reject 12.6" is
a complete answer. A rejection does not invalidate the ticket: each ticket names
which sections survive an overrule.

**What ratifying means.** The map's `Notes` and `Decisions so far` currently
carry these as decisions. Ratifying moves them from "agent assumption awaiting a
signature" to binding. Rejecting means the ticket is amended, not deleted.

---

## The one that gates the others

**Ticket 12, item 6 — the approval gate. Read this one first.**

*What replaces undo as the recovery mechanism?* proposes **dropping "auto-run
arbitrary code with no approval gate"** in favour of "auto-run **scene-only**
code; explicit approval for full-power code".

It made that call because the original premise died: research established that
Python-initiated operators **never push undo** and that undo reaches only local
`bpy.data` — so there is no recovery mechanism that can cover arbitrary code, and
"just press Ctrl+Z" was never a real gate.

Three other artifacts depend on this one:

| Depends on 12.6 | Effect if you reject it |
|---|---|
| The map's **Destination** was amended to the weaker wording | reverts to unscoped `bpy` |
| Ticket 13, *The capability boundary for model-authored code* | loses its premise entirely |
| Ticket 10, item 3 — the `{capability}` line in the system prompt | line is deleted, not rewritten |

**Ticket 13 has now closed, and it measured the answer instead of arguing it.**
`tools/capability_probe.py` makes 77 attempts against a reference implementation
of the proposed guard: **49 denied, 2 ESCAPED, 11 blocked by absence or type, 15
allowed**. I re-ran the probe and reproduced those numbers exactly.

The two escapes matter more than the count. Both come from **introspection**
(`()._\_class\_\_.__subclasses__()` reaching `os`, and a proxy leaking its own
`globals`), not from any named capability path — so the guard closes the paths a
model actually takes by mistake, and does not close a payload that intends to
leave. Ticket 13 states this plainly: the guard is **hygiene, not containment**,
and the Full access gate is therefore a **consent surface, not a containment
boundary**.

That amends ticket 12's own wording, which claimed the promise "becomes true
rather than decorative". So the honest form of 12.6 is *enforced for the direct
paths, explicitly not a sandbox*. If you want a real gate rather than a curated
namespace, this is the moment to say so: an OS sandbox or a process-isolated
executor is a different design, and one of them (process isolation) conflicts
with the map's no-IPC decision.

Ticket 13 also **corrects ticket 12's capability list against the installed
build**: `Text.write()` is not a file capability at all, and
`bpy.ops.script.python_exec` does not exist in 5.2.2 (`python_file_run` does).

---

## The rest, grouped by how expensive they are to change later

### Security — decide before anything ships

| # | Decision | The alternative, and what it costs |
|---|---|---|
| **05.1** | The API key is written **unencrypted into `userpref.blend`** by default; an env var overrides it. `SKIP_SAVE` was *verified* not to prevent persistence. | Env-only (on macOS this means never launching Blender from the Dock) or a `keyring` wheel (not bundled; adds a dependency). |

### User-visible behaviour you may disagree with on taste

| # | Decision | The alternative, and what it costs |
|---|---|---|
| **04** | JSON under `extension_path_user(..., "conversations")`, scoped **per `.blend` path** with alias-on-Save-As; 200 messages / 1 MiB, whole-turn pruning; never inside a `.blend`. | The ticket calls per-file scoping **its own most contestable choice**: a project with many `.blend` files fragments its history. Alternatives considered and rejected were a global stream, carrying across files, and refusing until the user chooses — all risk stale scene references. |
| **05.2** | `Model` ships with **no default**; Send refuses until it is filled in. | Pinning a model string. Rejected because the session could not verify the string exists. |
| **06.1** | `purpose` is a **required** field on `run_blender_python` — the only field added purely for trust and undo labelling. | Drop it to optional; no other contract changes. |
| **06.4** | `get_scene_info` has a scope enum and 25/100 limits — deliberately **no "give me everything" mode**. | Larger limits, or an unrestricted mode; both reintroduce the scene dump the design avoids. |
| **09.3** | The ask point is **before the first mutating call**, and `purpose` is **admitted to be the only mechanical gate**. | A stronger gate would need the capability boundary (13) — or 12.6 rejected outright. |

### Mechanics — reversible, but their evidence is thin

| # | Decision | Note |
|---|---|---|
| **12.1** | One undo step **per turn**, not per call, pushed **at the end in a `finally`**. | Rests on `-b` probes and timer probes, **not** on the GUI confirmation the ticket was asked to fold in. Ticket 15 exists for exactly this. |
| **12.2** | The recovery `.blend` copy is **off by default**, opt-in, once per session. | |
| **12.3** | The `get_scene_info` pre/post diff is the before-image — honest at **datablock** level, not property level. | |
| **12.4** | Global Undo off **pauses** auto-run; edit mode likely does too, **pending a probe**. | The ticket calls edit-mode "the item most likely to be wrong". |
| **06.2** | A **fresh namespace per call**, not a persistent one. | Called the choice "most likely to annoy a user who wanted console-like state". |
| **06.3** | The 8,000-char cap, split 4,000 / 2,000 / 2,000. | |
| **09.1** | Caps: 8 rounds, 24 calls, stop on a repeated call signature, stop after 3 consecutive all-failing rounds. | |
| **09.2** | **Report, never unwind** partial application. | The turn-end push is the only revert. |
| **09.4 / 09.5** | Synthetic `cancelled` tool results for unexecuted calls; **no automatic retry anywhere**. | |
| **10.1** | A `guide` kind added to `get_rna_info` — **an amendment to ticket 06's enum**. | If refused, the fallback is a **fourth tool, not a fatter prompt**. |
| **10.2** | Corrected undo rationale: one change per call is for **legibility and blast radius**, never per-call revertibility. | |
| **10.4** | The live summary is injected per turn as a second `system` message — authoritative at capture, **stale after any mutation**. | |

### Bets made on documentation rather than measurement

| # | Decision | The risk, in the ticket's own words |
|---|---|---|
| **11.1** | Accepting Blender's documented **"unsupported"** verdict on the long-lived thread + repeating timer as decisive, **without field-testing the crash rate**. | The ticket calls this **"the single largest unverified risk"**. It ruled on docs and precedent, not measurement. |
| **11.2** | A standalone `_worker.py` launched via `sys.executable`, newline-delimited JSON over stdin/stdout, rather than `multiprocessing`. | Launch-to-ready measured at **0.021 s**; IPC two orders of magnitude below one timer tick. |
| **11.3** | `proc.kill()` as the terminal Stop path. | |
| **11.4** | Visible failure over any silent non-streaming fallback. | |

---

## The last two decisions, both fresh

### 13 — The capability boundary for model-authored code

Eight items; the two that carry weight are **8** and **1**.

| # | Decision | Note |
|---|---|---|
| **13.8** | **Accepting the guard as hygiene rather than containment**, and amending the map's provisional "auto-runs scene-only Python" clause accordingly. | The keystone — it is 12.6 restated with the measurement attached. |
| **13.1** | The contents of the allowlist constant, **including `numpy`**, which needed its own file-function deny because `numpy.save(...)` writes a file on its own. | `statistics.sys`, `random._os`, `uuid.os`, `dataclasses.inspect` are all reachable the same way, which is why raw module objects must be proxied. |
| **13.2** | Dropping the `sys.meta_path` finder, on a measured `numpy`/`contextvars` false positive. | |
| **13.3** | The corrections to ticket 12's capability list. | `Text.write()` dropped, `Image.save` kept, `python_exec` corrected. |
| **13.4** | Adding `capability_denied` to ticket 06's `error.kind` enum, with `CapabilityDenied` subclassing `AttributeError`. | The subclass choice is so `hasattr`/`getattr(...)` default probing still behaves. |
| **13.5** | The gate's UX: the name **`Full access`**, per-session scope, a two-button denial row, and the receipt sentence. | |
| **13.6** | **Accepting a breakage**: `node.image = D.images.new(...)` raises `TypeError` because the image is a proxy. | A real scene operation the guard breaks. The mitigation is a rewritten error message, explicitly **not** silent unwrapping. |
| **13.7** | The constant's home at `blender_copilot/capability.py` plus an anti-drift test. | |

### 14 — How a conversation degrades as context grows

Five items. The mechanism is: **no model-authored summary**; drop, plus a
one-line marker, plus a live summary, plus re-reads. This is the ticket that
decides what the agent *forgets* and what the user *sees about it*.

| # | Decision | Note |
|---|---|---|
| **14.1** | No model-authored summary — drop, one-line marker, live summary, re-reads. | The cheapest mechanism, and the one that cannot hallucinate. |
| **14.2** | Eviction order: old tool results first, then call/result pairs, then whole turns — by **cost × reconstructibility**, not by age, with `MIN_KEEP_TURNS = 2`. | |
| **14.3** | **48,000 UTF-8 bytes** of prunable history, a **3 bytes/token** divisor, and one exception to "no automatic retry": a single re-issue on `context_length_exceeded` at `budget // 6`. | The divisor is a planning constant — no tokeniser is installable from Blender's bundle, which the ticket verified. |
| **14.4** | **Degradation is a projection and is never written back.** The store stays lossless wire format except for ticket 04's retention cap. | |
| **14.5** | The additive amendments: a `KIND_NOTE` in ticket 08's kinds, a **derived** header chip, `meta.retention` / `meta.context_trim` in ticket 04's file, and the live summary as the **trailing** message. | That last one **supersedes ticket 09 §4's `messages[0]` form**, which would have destroyed prefix caching on every request and contradicted ticket 10. |

The design principle worth reading in full: the panel shows the user's record,
the model sees the degraded view, and **the difference is on screen** — a chip
saying trimming is in effect, and a per-turn note saying which turns went.
Silent forgetfulness would be a trust bug.

---

## Not on this list: what needs eyes, not a signature

**Ticket 08, *How a conversation is laid out and controlled*, is a prototype
ticket.** Ratifying it is meaningless — it was never built to be signed, it was
built to be *looked at*. The list is in the ticket; the short version is box
chrome versus usable width, the ~34-character inset, page size 34, receipt
placement, `Ctrl+Alt+Z` discoverability, and pressing `Show code` once.

**No instance has seen the panel.** Launching a GUI is forbidden by
`AGENTS.md`, so every visual claim in tickets 07 and 08 is unverified by
construction. This is the largest outstanding body of evidence and only a human
can close it.

---

## Two evidence gaps a human must close

1. **Ticket 15 — *Confirm the four undo cases in a GUI*.** Status is
   `human-required` and it is excluded from frontier scans by protocol, not by
   convention. Run `tools/undo_probe.py` in the installed Blender 5.2.2 and
   record the observed behaviour. **If case 2 behaves differently than
   predicted, ticket 12 §1 is wrong and the whole push discipline has to be
   rewritten** — that is the negative control, and it is the reason this ticket
   exists rather than being an afterthought.
2. **The visual pass on ticket 08.**

## Known limits of this worksheet

- Ticket 08 is excluded above, correctly — it needs eyes, not a signature.
- Tickets 13 and 14 closed *after* this worksheet was first drafted and are
  folded in above. Their asks inherit everything else: **13.8 in particular is
  downstream of 12.6**, so ratify 12.6 before 13.8 or the second one is moot.
- Tickets 16, 17 and 18 were graduated by the second sweep and carry no
  decisions yet. **Ticket 16** (*The first live send against a real provider*) is
  `human-required` — it needs a credential, and until it is resolved **every
  wire-level claim in this document is untested**: the loop, the prompt, the
  transport and the context projection have between them never made a real HTTP
  request.
- Two claims were verified against the **5.3.0-alpha source clone** rather than
  the installed 5.2.2, and ticket 05 flagged this itself rather than glossing
  it. That is the one place a version conflation was risked.
