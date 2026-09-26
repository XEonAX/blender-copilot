# 06: Context projection

**What to build:** long conversations that keep working. When the history no longer
fits, the old material is dropped in a way the model can still reason about, a
fresh summary of the live scene rides along, and the panel says when trimming is in
effect — so the agent never becomes quietly forgetful.

**Blocked by:** 02 (One tool call, end to end)

**Status:** resolved
**Triage:** ready-for-agent

- [x] A conversation long enough to exceed the budget still gets answered correctly.
      **Evidence:** `tools/trim_panel_probe.py` — one real GUI turn over a 29,532-byte
      record against the addon preference's own minimum (8 KiB): the turn completes, the
      cube moves, the request's prunable part is inside the budget, and the history is
      still sendable. `tests/test_conversation.py` runs the same shape at a 900-byte
      budget, through two rounds and a real tool call.
- [x] Old tool output is elided before whole turns are dropped, and eviction is by cost times reconstructibility rather than age.
      **Evidence:** `tests/test_context.py` — with a budget that one elision can satisfy,
      exactly the *largest* result is elided and no turn is dropped; with six large results
      at a 700-byte budget all five pairs go (`calls_dropped == 5`) **before** any whole
      turn. **One consequence the decision did not state, found here:** because the ladder
      is exhaustive (pairs always before turns), a projection that dropped a turn never
      contains a surviving elided stub — so §5's expected note text, "3 tool results
      elided, 2 turns dropped", cannot occur. The report counts only the stubs that reach
      the wire (asserted in both test files), so the note never claims the model saw
      something that was subsequently dropped.
- [x] The live scene summary is the **last** message and the stable prompt stays first, so the provider's cache prefix survives a turn.
      **Evidence:** checked on the real request in the GUI probe (`request[0]` is the base
      prompt, `request[1]` the head marker, `request[-1]` the summary) and in the CPython
      suite for both the first request of a turn and a later round after a tool result.
      **Split:** the *placement* is measured; a cache *hit* is not, and cannot be without a
      live response's `prompt_cache_hit_tokens`. No command in this session made one.
- [x] A dropped turn leaves a visible marker, so a later reader can tell a gap from silence.
      **Evidence:** the head marker is a `system` message naming the turns, the messages and
      (when that is what went) the tool results — asserted in `plan`'s projection, on the
      wire request, and in the probe; the note's detail names each dropped turn with its
      prompt and its message count.
- [x] The panel shows when trimming is in effect, and expanding that says which turns went.
      **Evidence:** `logs/context-trim.png` (the header chip `✂ trimmed` and the note with
      its expander) and `logs/context-trim-expanded.png` (the detail lines), photographed
      in a real GUI session; `tools/panel_draw_smoke.py` draws the note expanded **and**
      collapsed in all three layouts and asserts the expander. **Split:** the photographed
      run is elision-only, so its expander reads `details`, not `which turns` — the
      turn-naming label and the dropped-turn detail lines are drawn by the smoke (with the
      demo fixture's note) and asserted at the conversation level.
- [x] The stored transcript is **unchanged** by trimming — the record keeps everything the model no longer sees.
      **Evidence:** `long.history[:len(seed)] == seed` and "grew by exactly the live turn"
      in the CPython suite; in the GUI probe the request's prunable part is under budget
      while all 24 seeded messages are still byte-for-byte in `session.history` **and in
      the conversation file on disk** — the model's view is the smaller of the two by
      design, and that asymmetry is what the note exists to make visible.
- [x] The budget is derived from the provider's real limit rather than the assumed floor the design was written against.
      **Evidence:** `context.derive_budget_bytes` is §1's own reserve stack and divisor;
      `tests/test_context.py` shows that the same function handed the **32k floor**
      reproduces the ratified 48,000 (48,212, within 0.5%), and that the shipped default is
      that arithmetic on ticket 16's 1M-token window — 2,958,356 bytes. The output reserve
      is read from `transport.MAX_TOKENS` rather than copied, so it cannot drift from the
      request it is reserving for.
      **Said out loud, because it changes what the feature does in practice:** at that size
      the store's own 1 MiB cap binds first, so the projection only bites for a single
      oversized turn (`store.prune` never drops the newest) or once a user lowers the new
      `context_history_kib` preference. The mechanism is exercised by the tests and by the
      probe at the preference's shipped minimum, not by ordinary use.

**Context:** *How a conversation degrades as context grows* fixes the eviction
order, the byte-based measurement and the trailing-summary placement, and
*The first live send against DeepSeek* **confirmed** that a trailing system message
is accepted — so the fallback that ticket documented is not needed. The provider
offers a far larger context than the design assumed, so treat the current budget as
a floor to revisit, not a target.

## Answer

### Decisions this transcribes (ratified; not re-decided)

Found by searching `.scratch/blender-copilot/issues/` for the two titles the ticket
names:

- **`14-conversation-degradation.md`** — §1's projection shape and its byte
  measurement (UTF-8 bytes of the wire JSON, compact, 3 bytes/token); §2's eviction
  order with its cost × reconstructibility rationale and `MIN_KEEP_TURNS = 2`;
  §3's "no model-authored summary" and the head marker's wording; §4's invariants
  I1–I10 plus the named adversarial fixtures and the seeded fuzz it asks for; §5's
  `KIND_NOTE`, the **derived** header chip, `meta.context_trim` and the retention
  notice; §6's "the panel shows the user's record, the model sees the degraded view".
- **`16-first-live-send.md`** — the confirmation that a **trailing `system`** message
  is accepted, so I8 stands as written and the fallback ticket 14 documented is *not*
  used; plus the provider facts (1M-token window, thinking billed as completion
  tokens) that box 7 turns into the derived budget.

Three things in §1 changed shape under implementation. All three are recorded here
rather than smoothed over:

1. **The budget is now derived (box 7).** §1's arithmetic, unchanged, with
   `WINDOW_TOKENS` set to ticket 16's reading instead of the assumed 32k floor.
2. **§5's example note cannot occur.** The ladder is exhaustive — step 2 drops
   *every* call/result pair it can before step 3 takes a single whole turn — so a
   projection that dropped a turn never contains a surviving elided stub, and the
   note therefore reports elisions **or** turns, never both. §5's "model saw 3 tool
   results elided, 2 turns dropped" describes a state this order cannot produce.
3. **§1's pre-flight warning is not built.** It is not one of the seven boxes, and at
   the derived budget it needs a single turn over ~5.9 MB.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/context.py` | **new**, ~470 lines, **no `bpy`**: the budget derivation, `plan()`, `validate_projection()`, the head marker, the note's sentence and detail, and `trim_meta` |
| `blender_copilot/conversation.py` | `KIND_NOTE`; `build_messages` sends `[system(base), *projection, (user), system(summary)]`; `budget_bytes()`, the derived `trimmed` property, `_note_trim()` (one note per turn), `turn_start` protection, `restore(history, meta)`; `attach(..., budget=)` |
| `blender_copilot/stream.py` | `history_budget_bytes()` — the addon preference or the derived default — wired through the existing `attach` seam |
| `blender_copilot/panel.py` | the `context_history_kib` preference (0 = derived, 8–512 KiB to pull it down); the derived `✂ trimmed` chip on the scope row; `_draw_note` with its expander |
| `blender_copilot/scope.py` | `restore(history, meta)` keeps the record's trim counters; `persist()` writes `meta.context_trim` beside `retention` |
| `tests/test_context.py` | **new**, 60 checks: the derivation, the order, the floor, I4, the 512-byte threshold, the tie-break, the orphans, the validator's positive controls, idempotence, monotonicity, and 300 generated conversations × 12 budgets (3,600 projections, all validated) |
| `tests/test_conversation.py` | +41 checks, 300 total: the whole projection through the loop — the marker, the summary's position, the note (once per turn), the store untouched, the chip |
| `tools/panel_draw_smoke.py` | +4 checks, 26 total: the note drawn expanded and collapsed in every layout, the chip appearing and disappearing with the budget, the preference's shipped default and ceiling |
| `tools/trim_panel_probe.py` | **new**, 24 checks in a real GUI session: a seeded 29,532-byte record against the preference's own minimum, one real turn, the request's shape, the file on disk, and three screenshots |

The projection lives in its own module because it is the one part of this system that
is pure: `plan(history, budget_bytes, current_turn_start) -> (sent, report)` reads a
list of dicts and returns a new one. Nothing in it knows about `bpy`, the panel or the
store's files, which is what makes "the record is unchanged" a property of the shape
rather than a promise about behaviour.

### Evidence

**Gate 1 — the CPython suite** (300 checks; 259 at ticket 05's close, +41 here):

```
$ python3 tests/test_conversation.py; echo "EXIT=$?"
all checks passed
EXIT=0
$ grep -c '^ok ' logs/context-gate1.txt
300
$ awk '/-- the projection --/,0' logs/context-gate1.txt | grep -c '^ok '
41
```

**Gate 2 — the draw smoke**, gated on its token, never on Blender's exit status:

```
$ python3 tools/bounded_run.py 60 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/panel_draw_smoke.py; echo "RC=$?"
ok   the trim note draws expanded and collapsed, and wraps in both
ok   the header chip says the model is not seeing all of it:
     ['prototype', 'running code', 'unsaved — session only', '✂ trimmed']
ok   and the chip goes again when the same record fits the budget
ok   the history budget is a preference that ships derived and can only be pulled down (0-512 KiB)
SMOKE OK
RC=0
```

**The projection's own suite — `tests/test_context.py`** (`python3
tests/test_context.py`, exit 0):

```
ok   ticket 14 §1's arithmetic, on the 32k floor that budget was written against, reproduces the ratified 48,000
ok   the shipped default is that same arithmetic on the real 1M window
ok   a result of exactly 512 bytes is not worth eliding
ok   513 bytes is, so the threshold is where ticket 14 put it
ok   step 3 drops the oldest turns first
ok   and stops at the floor instead of going lower
ok   the validator catches a result whose call is missing (I1)
ok   the validator catches a call with no result (I2)
ok   the validator catches a projection in the wrong order (I5)
ok   the validator catches a duplicated message (I5/I7)
ok   300 generated conversations x 12 budgets: 3600 projections, every one sound
ok   and none of them over its budget: 0 exceptions
all checks passed (60)
```

**A real turn over a record that does not fit — `tools/trim_panel_probe.py`**, in a
GUI session with only the model faked (`SMOKE OK`, 24 checks; `TRIM |` lines):

```
$ python3 tools/bounded_run.py 150 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --python tools/trim_panel_probe.py
TRIM | saved a probe scene to /tmp/bc-t06-probe.blend
TRIM | seeded 24 stored messages, 29532 B, against a 8192 B budget
TRIM | ok   the record is over its budget before anything is sent
TRIM | ok   and the chip is already on, because it is derived rather than stored
TRIM | ok   the turn was answered even though the record did not fit
TRIM | ok   the cube changed, so the turn really ran
TRIM | ok   nothing is left unanswered, so the history is still sendable
TRIM | ok   two rounds went out, so the projection was rebuilt mid-turn
TRIM | ok   the request went out with the base prompt at index 0
TRIM | ok   the live scene summary is still the last message
TRIM | ok   and the head marker sits between them
TRIM | ok   the prunable part of the request fits the budget
TRIM | ok   and it is a sound projection of the store as it stood then
TRIM | ok   the second round is a sound projection too, turn in flight and all
TRIM | ok   the stored transcript is unchanged by the trim
TRIM | ok   and every seeded message is still there, byte for byte
TRIM | ok   the conversation was written to a file of its own
TRIM | ok   and the file records that the model saw less than it holds
TRIM | meta on disk: {"context_trim": {"first_trim_at": "2026-09-26T08:08:44Z",
       "turns_elided_total": 0, "last_trim": {"turns": 0, "messages": 0,
       "tool_results_elided": 6}}, "retention": {"turns_dropped": 0, ...}}
TRIM | ok   the panel has exactly one trim note
SMOKE OK
```

**The panel, photographed** (a screenshot is evidence, and each says what it does not
show). `logs/context-trim.png` is the note collapsed with a `details` expander and the
chip in the header; `logs/context-trim-expanded.png` is the same note expanded,
listing the budget, the eviction order and each elision with its byte cost;
`logs/context-trim-full.png` is the *unedited* six-turn state, in which the note is
below the fold — a Panel cannot scroll, so the two note photographs were taken with
the transcript reduced to the note and the live turn (the same trick ticket 05 needed
for the receipt), and that reduction is printed in the log.

**Regressions, all green** after the change: `tests/test_store.py` (75),
`tests/test_read_only_tools.py` (110), `tools/loop_panel_probe.py` (28 checks,
`SMOKE OK`), `tools/persistence_gui_probe.py` (`SMOKE OK`).

### Two bugs the screen caught that the suites did not

Both are the kind the stub layout cannot see, which is part of why the probe exists:

- **The chip clipped the status row.** Drawn first on the status row, three labels
  middle-clipped `prototype` to `prototy…` — visible in the first screenshot and in no
  assertion. The chip now shares the scope row, and the screenshot shows
  `prototype idle` intact.
- **The note's first line was clipped** by the shared wrap budget: `✂` and `—` are
  wide glyphs and the note sits inside a box, so it wraps a few characters earlier
  (`NOTE_WRAP_INSET`), measured by looking at `logs/context-trim.png`.

And one bug the *probe* caught that the suite missed, now covered by both: the per-turn
note latch was reset in `begin_turn`, which wipes the note the first request had just
written (the first request of a turn is built before the turn opens) — so a turn with a
trimming round 2 noted twice. The GUI run showed two notes; `tests/test_conversation.py`
now asserts one per turn and two across two turns.

### Deliberately not built, and why

- **§1's pre-flight warning** ("the in-flight turn alone exceeds 2 × budget") — not in
  the boxes, and at the derived budget it needs a turn over ~5.9 MB.
- **§1's one permitted automatic re-issue** on `context_length_exceeded`. This is a real
  gap between ticket 14 §1 and this ticket's boxes, and it is left unbuilt on purpose:
  ticket 16 **could not force** that error (a 1M-token limit means megabytes of
  padding), so its trigger is still a guess about an unseen shape — *and* at the derived
  budget a 1 MiB store cannot produce the error at all, so the code would be unreachable
  as well as unverified. It belongs with whoever next touches the transport error path.
- **Reading `meta.context_trim` back.** It is written (measured above) and never read:
  §5 says informational, and the chip is derived from the projection, so a stored flag
  cannot drift from reality.

### What I could not verify

- **No live request was made.** Nothing in this ticket touches the network path, so
  `tools/transport_smoke.py` was not run and no money was spent. The two wire-level
  facts it leans on are ticket 16's measurements: a trailing `system` message is
  accepted, and `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` are reported.
- **The cache-prefix saving itself.** The placement that produces it is asserted; the
  saving is not, because reading it needs a live response. Ticket 14's `Unverified` note
  is half-closed rather than closed.
- **The 3-byte/token divisor** remains a planning constant — nothing in Blender's bundle
  can tokenise (ticket 14 §1) — so its error cost is bounded by the down-shift that is
  not built, not by a measurement.
- **A mixture of elided stubs and dropped turns in one note**, since the order cannot
  produce one (see above). The note's format handles all three counts; only tests
  exercise that shape.

### For a human, if anyone wants to change it

1. **The preference's range can only pull the budget down** (8–512 KiB, and the derived
   default sits above the ceiling). That range is ticket 14 §1's, transcribed rather than
   widened — but a user pointing this add-on at a larger model than DeepSeek has no lever
   upward, and nobody ratified that ceiling against a 1M-token window.
2. **The derived default never binds against the store's cap** (2.8 MiB budget, 1 MiB
   store). That is box 7 taken literally, and the honest consequence of a real 1M-token
   provider — but it does mean the chip and the note are, in ordinary use, invisible,
   which is worth knowing before anyone calls the feature exercised.
3. **The trim notice's position** (a box at the head of the turn it describes) and its two
   labels (`details` / `which turns`) are my choices; ticket 14 fixes the sentence, not the
   layout.
