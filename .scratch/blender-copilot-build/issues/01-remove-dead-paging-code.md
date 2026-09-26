# 01: Remove the dead paging code

**What to build:** nothing a user can see — this is the prefactor, done first so the
rest of the build does not read two contradictory designs. The panel used to page
its transcript; that was rejected and the panel now renders the whole conversation
and lets the sidebar region scroll. The paging machinery is still present, still
registered, and still covered by unit checks that assert behaviour the panel no
longer has. Delete it, checks included.

**Blocked by:** None (can start immediately)

**Status:** resolved
**Triage:** ready-for-agent

- [x] The paging operators are gone and nothing registers them.
- [x] The page-packing helper, the page-size constant and the session's page cursor are gone.
- [x] The unit checks that covered paging are removed with them, so no check asserts behaviour the panel no longer has.
- [ ] The whole transcript still renders and the sidebar region still scrolls.
- [x] The panel draw check passes and prints its success token.
- [x] Nothing else in the extension changed behaviour — this is a deletion, not a redesign.

## Answer

The pager is deleted, not parked. Six files, and in the package itself **every added
line is a comment** — the diff is 125 deletions against 31 insertions, and the added
package lines are the two comment blocks quoted below.

### Decisions this transcribes (ratified; not re-decided)

Ticket 01 names no ticket by title, so the decisions were found by search. They are:

- **`08-panel-conversation-ux.md`, §"Paging", SUPERSEDED 2026-09-26 by the human visual
  pass:** *"emit everything and let the region scroll … The owner saw the pager
  running and rejected it."* The mechanical consequence is that `PAGE_LINES = 34`, the
  `Older`/`Newer` operators, the `→ N earlier` marker, the `_cost` estimate and its
  "expanding a block never reflows the page" regression check all lose their subject.
- **`11-http-transport-thread-or-subprocess.md:204`** names this exact pass:
  *"`blender_copilot/conversation.py` keeps ticket 08's dead pager (`page_view`, `_pages`,
  `PAGE_LINES` and the two page operators) untouched — that removal is its own pass,
  tests included, as that ticket says."*
- **`map.md:56`** records the reversal as settled: *"the bounded pager is **gone** (the
  whole transcript renders and the sidebar region scrolls)."*

**Two ratified clauses go vacuous, and I did not edit either ticket** (one owner per
file). Neither is wrong; both were correct about a world that had a cursor:

1. **`18-multi-panel-ownership.md:104-108`** rules that the paging cursor *stays shared*
   so a panel is a pure function of process state, against rejecting a per-panel
   cursor. There is no cursor to share now. Ticket 18's core — per-process state, a
   second panel as an identical shared view — survives intact; only its paging clause
   and its §6 item 3 annotation to ticket 08 ("the statement that paging is
   process-global") lose their referent. "Shared view" now means one transcript whose
   scroll position belongs to each region, which Blender owns and the addon cannot see.
2. **`14-conversation-degradation.md:288-294`, "6. Interaction with paging — confirmed"**
   is confirmed *about* the pager. Its substance ("the panel shows the user's record; the
   model sees the degraded view", `plan()` never paged) is unaffected, and `wire_messages`
   already carries the live half of it.

A human may want to strike those clauses; nothing in the build depends on them.

### What changed, and where

| file | change |
|---|---|
| `blender_copilot/conversation.py` | `PAGE_LINES` and its comment gone; `Conversation.page` cursor gone (from `__init__`, `begin_turn`, `clear`); the whole `# -- rendering: paging --` section gone: `_cost`, `_pages`, `page_view`, `older`, `newer`, `newest` (53 lines) |
| `blender_copilot/panel.py` | `BLENDER_COPILOT_OT_page_older` and `BLENDER_COPILOT_OT_page_newer` gone; `_draw_boxes`' comment now describes the live design instead of naming the dead one; `_turns` docstring said "the page", now "the transcript" |
| `blender_copilot/__init__.py` | the two classes dropped from `_classes`, so nothing registers them |
| `tests/test_conversation.py` | the 9 paging checks gone (8 in `# paging`, 1 in `# stable paging`); 3 guards added that the paging surface stays gone |
| `tools/panel_draw_smoke.py` | `session.newest()` dropped; the paged-kind probe replaced by a whole-transcript one |
| `blender_copilot/__pycache__/` | deleted — stale byte-code carrying the deleted names |

`_cost` is not named in the ticket, but it existed only to price a page, so it went with
`_pages`. `page_view`/`older`/`newer`/`newest` were the pager's API with no remaining
caller.

The new guard in `tests/test_conversation.py`:

> ```
> # The pager lost at the 2026-09-26 visual pass: the whole transcript renders and
> # the sidebar REGION scrolls. These guard the public surface, because the failure
> # they prevent is the *unused* pager - the thing that let two contradictory
> # designs sit in one file and both read as live.
> ```

The new `_draw_boxes` comment, which replaces the paragraph that named the dead code:

> `# No pager, and none left to switch back to. The human visual pass of`
> `# 2026-09-26 rejected bounded pages in favour of emitting everything and`
> `# letting the sidebar REGION scroll - the panel cannot scroll itself, but`
> `# the region it lives in can. So the whole transcript renders here.`

### Evidence

**Red first.** The guards were written before the deletion and failed against the
intact pager:

```
$ python3 tests/test_conversation.py
ok   user lines are marked
ok   assistant lines are marked
Traceback (most recent call last):
  File "…/tests/test_conversation.py", line 168, in <module>
    check("no page-size constant", not hasattr(conversation, "PAGE_LINES"))
AssertionError: FAILED: no page-size constant
```

**Gate 1 — the CPython suite** (62 checks pass; it was 68 before: 9 paging checks out,
3 guards in):

```
$ python3 tests/test_conversation.py
ok   clear empties the conversation

all checks passed
$ python3 tests/test_conversation.py | grep -c '^ok '
62
```

**Gate 2 — the panel draw check**, gated on its token and never on the status:

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
ok   variant=log      expanded=False widgets=59 boxes=5 longest_label=39
ok   variant=log      expanded=True  widgets=59 boxes=5 longest_label=51
ok   variant=boxes    expanded=False widgets=97 boxes=16 longest_label=34
ok   variant=boxes    expanded=True  widgets=118 boxes=16 longest_label=61
ok   variant=external expanded=False widgets=37 boxes=5 longest_label=36
ok   variant=external expanded=True  widgets=37 boxes=5 longest_label=36
ok   the transport failure block draws, and only when it should
ok   the whole transcript draws: error block and running tool included
ok   Stop replaces Send only while a turn is in flight

all draw bodies ran
SMOKE OK
Blender quit
BOUNDED | finished on its own in 1.6s | rc=0
```

`grep 'SMOKE OK\|SMOKE FAILED\|BOUNDED'` on the saved run shows exactly
`12:SMOKE OK` and `16:BOUNDED | finished on its own in 1.6s | rc=0` — no
`SMOKE FAILED`.

The third-from-last `ok` is new and is what replaced the paged-kind probe: one
`_draw_boxes` pass must contain a label carrying the `ERROR` icon (the demo's **oldest**
assistant block) and one carrying `TIME` (its **newest** row, the running tool). Both
ends of the transcript in a single draw is the evidence that there is no page to hide
one on.

**Checkbox 1 beyond grep — a bounded runtime probe** (`/tmp/t01_opcheck.py`, one
`register()`/`unregister()` cycle on a factory startup):

```
$ python3 tools/bounded_run.py 60 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python /tmp/t01_opcheck.py
OPCHECK {'BLENDER_COPILOT_OT_page_older': False, 'BLENDER_COPILOT_OT_page_newer': False,
         'BLENDER_COPILOT_OT_send': True, 'BLENDER_COPILOT_OT_stop': True,
         'BLENDER_COPILOT_OT_clear': True, 'BLENDER_COPILOT_PT_panel': True}
OPCHECK MODULE []
OPCHECK OK
BOUNDED | finished on its own in 1.2s | rc=0
```

**No paging reference survives in code, tools, tests or docs:**

```
$ grep -rn 'page_older\|page_newer\|PAGE_LINES\|page_view\|_pages(\|_cost\|\.page\b\|newest()\|session.older\|session.newer' blender_copilot tools tests README.md docs
tests/test_conversation.py:168:check("no page-size constant", not hasattr(conversation, "PAGE_LINES"))
tests/test_conversation.py:174:        for name in ("page_view", "older", "newer", "newest")
```

Those two hits are the guard itself. (`logs/*.txt` are stale transcripts that still
mention the pager; they are records of past runs, not code, and were left alone.)

**Box 6 — the diff is a deletion:**

```
$ git --no-pager diff --stat
 blender_copilot/__init__.py            |  2 --
 blender_copilot/conversation.py        | 53 ------------------------------
 blender_copilot/panel.py               | 42 ++++-------------------
 tests/test_conversation.py             | 42 ++++++++---------------
 tools/panel_draw_smoke.py              | 15 ++++++---
 6 files changed, 31 insertions(+), 125 deletions(-)

$ git --no-pager diff -U0 -- blender_copilot | grep -E '^\+' | grep -v '^+++'
+        # No pager, and none left to switch back to. …
+    """Group the transcript into turns: a user message starts one, assistant
```

Every added line in the package is a comment.

### Not ticked: "The whole transcript still renders and the sidebar region still scrolls"

The **renders** half is shown this session, above: one `_draw_boxes` pass draws both the
oldest and the newest message, and `_draw_boxes` iterates `conversation.session.messages`
with no bound. The **region scrolls** half is not shown by any command I ran, so I left
the box unticked rather than tick half a claim.

What is missing: a GUI run with a window. The region's scrollbar is Blender's, not the
addon's, and nothing in this diff touches the region, the draw order or the layout — but
"nothing in this diff touches it" is an argument, not a measurement. Ticket 08's recorded
human visual pass of 2026-09-26 has the measurement (*"had to scroll the sidebar to reach
Send"*, with the input still below the transcript at that time), which is why this is a
formality rather than an open question. I did not take a screenshot: the panel lives in
the N-sidebar, which the default startup has closed, and our tab may not be the active
one, so the probe would have needed its own layout setup and a `screencapture` that may
be permission-blocked — I judged that not worth the failure surface for a fact the
deletion cannot have changed, and stopped rather than guess. A human confirms it by
scrolling one long conversation in the sidebar.

### What else I could not verify

- That no *runtime* path reached the deleted methods other than the deleted operators and
  the deleted checks. Evidence is `grep` over all of `blender_copilot/`, `tools/` and
  `tests/`, plus the smoke run exercising every draw body and `register()` — not an
  exhaustive walk of the GUI.
- The `# stable paging` regression check ("expanding detail never reflows the page") is
  gone with no replacement, deliberately: there is no page to reflow. The claim it
  guarded is now trivially held by the region scrolling.
- The log variant's truncation checks (`visible_lines`, `VISIBLE_LINES`, 2 checks) were
  **kept**. They are not paging: `log` is still a live layout in the variant picker, so
  those checks assert behaviour the panel still has. If a reader expected them gone, that
  is the reasoning.
