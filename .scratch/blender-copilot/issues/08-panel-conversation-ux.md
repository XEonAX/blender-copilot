# How a conversation is laid out and controlled

Type: prototype
Status: resolved
Blocked by: none

## Question

With a panel that renders and repaints, decide how a real conversation reads and how the user controls it.

Decide, by building **two or three contrasting concrete layouts** and reacting to them rather than arguing abstractly:

1. How user turns and assistant turns are visually distinguished.
2. **How model-authored code and its output are shown.** Inline code block, collapsible, or a separate area? The user has to be able to read the code the model is about to run, even without an approval gate. **Constraint, verified:** `UILayout` has no rich text in 5.2.2 (`label` accepts only text/icon — no markup), so there is no in-panel code styling and no monospace control. Decide whether code belongs in the panel at all or in a companion Text Editor area bound to an addon-owned `Text` datablock.
3. How a running tool call is indicated, and what a finished one looks like afterwards.
4. How errors surface (a failed `bpy` call, a failed API call) without wrecking the transcript.
5. **The revert affordance.** Auto-run with no gate means the user's control is *after* the fact — so how do they see that the last run is undoable, and how do they walk it back? Note that Ctrl+Z is global and the agent has been pushing its own undo steps; decide whether the panel exposes its own revert control or simply teaches the user that Ctrl+Z walks the agent's work back.
6. Where Stop lives, and what it does to an in-flight tool call versus an in-flight stream.

Deliverable: the chosen layout and why the rejected alternatives lost.

## Answer

**Chosen: `boxes` — labelled user turns, one bordered box per assistant turn,
collapsed detail rows, bounded pages.** Built at `blender_copilot/panel.py`
(live switch: *Layout (prototype)* at the bottom of the panel). The other two
variants are in the same file and stay as a comparison surface: `log` (flat
prefixed text) and `external` (controls only; transcript in a Text datablock).

**What was verified, and how.** Headless probes against the installed 5.2.2:
`UILayout` has `box`, `row`, `separator`, `alert`, `label(icon=)` and 1033 valid
icon names (the 13 used were checked); `bpy.ops.ed.undo_history` exists and the
default keymap binds it to **Ctrl+Alt+Z**
(`.../5.2/scripts/presets/keyconfig/keymap_data/blender_default.py:865`), with
Blender's Edit menu already exposing `ed.undo`/`redo`/Undo History
(`.../5.2/scripts/startup/bl_ui/space_topbar.py:515-521`); `bpy.data.texts`
`new`/`clear`/`write`/`as_string` work. All three layouts' draw bodies run for
every message kind, expanded and collapsed, via a stub `UILayout`
(`tools/panel_draw_smoke.py`); 39 CPython checks pass; the manifest validates and
the package builds. **Not verified, needs eyes:** how it looks, the textbox, the
`Show code` area split, streaming smoothness, and the undo probe.

### 1. Role distinction
Role header with an icon (`USER` / `BLENDER`), plus structure: the user turn is
unboxed, the assistant's *whole turn* is one `layout.box()`. Rejected: colour (no
API), glyph prefixes alone (`log` — boundaries vanish once prose wraps), and
`alert` for roles (reserved for errors).

### 2. Code and its output
**Code does not really fit in the panel.** The default sidebar is 280 px; the
prototype's manual wrap budget is ~38 chars, minus the box inset ~34 — code lines
are 40-80 chars, there is no monospace, and overlong labels clip. So the panel
shows code *identity*, not code: `▸ Scale Cube 1.3x on Z · 10 lines`, expandable
inline for short snippets, and the full unwrapped text is mirrored into the
addon-owned **`Copilot Code`** Text datablock, with `Show code` opening a Text
Editor (splitting one if none). Rejected: always-inline full code (floods a
non-scrolling panel and still clips); a Text Editor as the *only* code surface
(loses adjacency to the tool call that ran it); never showing code in the panel.
Note the honesty point: with no approval gate the transcript is an **audit**
surface, not a review surface — reading before running is exactly what ticket
12's full-power approval buys.

### 3. Running vs finished tool calls
One permanent row per call, never removed: `TIME` + purpose + `running…` →
`CHECKMARK` + purpose, output collapsed behind a toggle; failure → `ERROR` +
`alert` + collapsed traceback. Rejected: live-streaming tool stdout (**impossible**
— exec is synchronous on the main thread and the event loop does not pump, so
nothing can repaint mid-call) and transient spinners (lose the receipt).
**Consequence for ticket 09:** the loop must be a per-tick state machine with one
tool call per timer callback; a blocking round-loop can never draw the running
state at all.

### 4. Errors
A failed `bpy` call → tool row with `ERROR` + `alert`, traceback collapsed inside.
A failed API call → its own transcript block (icon `ERROR`, request error, JSON
detail), turn marked incomplete, partial reply kept. Nothing auto-retries; the
user resends. Errors never clear or truncate the transcript. Rejected: status-bar
message (vanishes), panel-wide alert (destroys the turn's context), separate
error log.

### 5. Revert affordance
**Teach Ctrl+Z; do not add a Revert button.** Per mutating turn: a receipt
(`✓ Undoable · 1 object changed — Cube scaled on Z` +
`Ctrl+Z reverts that turn; Ctrl+Alt+Z opens Blender's undo history`) plus the
persistent coverage sentence from ticket 12. Rationale: push-at-end makes the
agent's step an ordinary *labelled* undo step, and Blender's native Undo History
already renders it — but the addon cannot query the top of the undo stack (ticket
12: zero undo RNA), so a one-click `Revert` would silently revert the user's own
last action whenever the agent's step is not on top. Degraded mode: Global Undo
off → persistent banner + Send disabled (auto-run paused). Rejected: custom
revert control (unreliable), an in-panel undo stack (duplicates Blender's),
per-tool-call steps (ticket 12: one step per turn).

> **SUPERSEDED 2026-09-26 by the human visual pass: the receipt moves under the
> streaming reply.** The turn's own step, attributed to the turn rather than
> standing above the input. Consequence to absorb: the receipt now **scrolls away
> with its reply**, so undo discoverability depends on finding the turn — and that
> sharpens with the paging change, since the transcript is no longer bounded.

### 6. Stop
One stable slot: **Stop replaces Send** while a turn is in flight. It cancels an
in-flight *stream* (worker `{"cmd":"cancel"}`, then `proc.kill()` after grace),
discards queued-but-unstarted tool calls, keeps partial text marked `[stopped]`.
It **cannot** stop an in-flight `run_blender_python`: exec runs synchronously on
the main thread, Blender processes no events while it runs, so the click is not
even delivered. The panel therefore shows `running code — cannot be interrupted`
instead of a dead button. A hung exec stays force-quit-only. Rejected: a second
Stop elsewhere, a modal hotkey (same delivery problem), pretending Stop kills
code (invites `while True:`).

### Rejected layouts
`log` is cheapest (49 stub widgets vs 78) but every kind reads as one wall of
prefixed text. `external` (27 widgets) gives real scrolling, monospace and
selection, but separates input from output, loses Enter-to-send and code/tool
adjacency; kept as the *escape hatch* for full history, not the conversation.
Rejected on paper: an in-panel `UIList` transcript (scrolls, but rows are
single-line, so wrapped prose and code are clipped — clicking each row to read it
is worse than the region scroll) and reverse-chronological order (fixes input
drift, makes the conversation read backwards for no gain once pages are bounded).

### Paging
A panel cannot scroll; the region can. Adopted: whole-message pages packed from
the newest end (`PAGE_LINES = 34`), `Older`/`Newer`, `→ N earlier` marker, newest
page always full. Page cost is computed as if details were collapsed, so
expanding a block never reflows the page (regression check added).

> **SUPERSEDED 2026-09-26 by the human visual pass: emit everything and let the
> region scroll.** The owner saw the pager running and rejected it. This also
> reverses the §"Rejected layouts" reasoning that bounded pages were what made the
> boxes layout viable, and it collides with the input-drift objection the ticket
> already raised under "reverse-chronological order" — which the owner then
> independently reported as *"I had to scroll the sidebar to reach Send"*. The two
> rulings need the order change proposed in the visual-pass section.

### What a human must still judge
- Box chrome vs. usable width; is ~34 chars the right inset; should short code
expand by default.
- Page size 34 + top-of-panel pager vs. just emitting everything and letting the
region scroll.
- Whether the receipt belongs above the input (chosen) or under the streaming
reply, and whether `Ctrl+Alt+Z` in a sentence is discoverable enough.
- Run `Show code` once — its area split was never exercised headlessly.

## Human visual pass, 2026-09-26

The panel was finally **looked at**, running on the installed 5.2.2, by the
project owner. This is the first visual claim in this effort checked by eye —
every panel claim up to now was structural (draw bodies executed against a stub
`UILayout`) or unverified.

Setup was verified *before* the look, so the looking was not spent on a broken
panel: manifest valid; every variant's draw body runs headlessly
(`tools/panel_draw_smoke.py`, three variants × expanded/collapsed, no exceptions);
the extension enables (`ENABLE: OK`, panel class registered, prefs resolve,
default variant `boxes`); symlinked dev loop pointing at this repo.

**Decided: the layout is `boxes` ("Role boxes").** Chosen by the project owner
after seeing it render. That settles §1's choice against the two alternatives
which were built and left switchable at runtime — `log` (flat, no borders) and
`external` (panel keeps only controls; transcript and code move to `Text`
datablocks).

A measured input to that decision, from the smoke test's counters: `boxes` costs
**78 widgets and 13 layout boxes** against `log`'s **49 and 4** (98 widgets once
a block is expanded); `external` is cheapest at 27. So the chosen layout is
meaningfully the most expensive of the three and was chosen anyway — recorded
that way because it is a real trade of density for legibility, not a free win.

**All five items answered, and two of them reverse a decision made above.**

| item | verdict |
|---|---|
| box chrome vs. usable width; the ~34-char inset | **right as it is** — keep it |
| short code expanding by default | not separately reported; current behaviour stands |
| paging vs. emitting everything | **emit everything, let the region scroll** — reverses the pager |
| receipt placement | **under the streaming reply** — reverses "above the input" |
| `Ctrl+Alt+Z` discoverability | not separately reported |
| `Show code` | **pressed, and it worked** — the one item with zero prior coverage, now closed |
| reaching the input | **had to scroll the sidebar to reach Send** |

**Two things the screenshots confirmed working**, which no headless test could
have reached: errors render as first-class red blocks (`⚠ Link red material` with
a red-bordered output strip), a running call reads as `◑ … running…`, and
ticket 12's coverage sentences render under the actions (`Ctrl+Z undoes one agent
turn.` / `Files, network and preferences cannot be undone.`).

**A tension to resolve.** "Emit everything" and "I had to scroll to reach Send"
pull against each other: the current order is header → transcript → receipt →
**input** → actions → coverage → variant picker, so an unbounded transcript pushes
the input *further* below the fold — exactly the problem just reported. The
synthesis that satisfies both rulings is to **move the input and its actions above
the transcript**, leaving the controls pinned at the top of the sidebar with the
conversation flowing and scrolling beneath them. Unusual for chat; correct for a
panel that cannot scroll itself. **Not implemented** — it changes the panel's
order, which is a design decision and not a tweak.

**A third finding, this one from ticket 17 rather than from eyes.** The screenshot
shows `running code — cannot be interrupted`. Ticket 17 has since measured that
`SIGALRM` *can* stop a pure-Python loop, `time.sleep` and a blocking `recv` (at
1.04× overhead), and *cannot* stop a long native call until it returns. So that
sentence is **half wrong**: true for a blocking C call, false for a Python loop.
§6's honesty contract needs to say which — it is the same class of error the
design has been careful to avoid everywhere else.

**Both rulings implemented in `blender_copilot/panel.py`, same day.** The panel
order is now header → **input → actions** → transcript → receipt → coverage →
variant picker, so the controls sit *above* the unbounded transcript and cannot
drift off the fold. Regression-checked after the change: `panel.py` compiles, all
three variants' draw bodies still run, the 42 conversation checks pass, and the
manifest still validates. `boxes` now costs **80 stub widgets** against 78 before
— the +2 is the Stop note below, which carries three labels instead of one.

**The receipt needed no move of its own.** Drawing it immediately after the
transcript *is* "under the streaming reply"; it only ever looked like it sat
"above the input" because the input happened to come after it. Moving the input
resolved that reversal by itself.

**Residual drift, recorded rather than glossed.** Everything *after* the
transcript — the receipt, the coverage sentences, the prototype picker — now sits
below an unbounded transcript and can be pushed out of reach. For the picker that
is harmless. For the receipt and the coverage sentences it matters, because
ticket 12's honesty contract assumes the user can see them. The fix, if wanted, is
to move the coverage block up with the controls; it was offered and not taken, so
it stays the owner's call rather than being decided here.

**The interruption claim was corrected, not reworded.** The panel now says
`running code — click not processed until it returns`, and adds that a blocking C
call can never be stopped while a pure-Python loop can be, *once a per-call budget
is enforced*. That follows ticket 17's measurement, and replaces a blanket
"cannot be interrupted" that was already half wrong — the budget is not built, but
the sentence no longer claims more than the mechanism can deliver.

## Comments
