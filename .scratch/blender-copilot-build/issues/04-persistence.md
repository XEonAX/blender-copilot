# 04: Persistence

**What to build:** conversations that survive. Close Blender, reopen the same file,
and the history is still there and still scoped to that file. Independent of the
tool loop, so this can run **in parallel with 02** rather than waiting for it.

**Blocked by:** None (can start immediately)

**Status:** resolved
**Triage:** ready-for-agent

- [x] A conversation survives closing and reopening Blender on the same file.
- [x] History is scoped per file path: opening a different file shows that file's conversation, not the previous one's.
- [x] Save As re-scopes to the new path without silently duplicating or losing the old history.
- [x] Nothing is ever stored inside the .blend itself.
- [x] An unsaved file keeps its conversation for the session only, and says so.
- [x] The cap prunes whole turns and never leaves a tool result orphaned from the call that produced it.
- [x] Clearing starts a new conversation; deleting everything requires an explicit confirmation and offers to reveal the folder.
- [x] Opening the file switches the scope, and the handler that does it survives a reload.

**Context:** *Where chat history lives* decided the location, the scope, the caps
and the pruning rule, and *verified* that a `Text` datablock persists into the
.blend — which is exactly why history must not live there. Two behaviours in that
ticket were measured rather than assumed; keep them.

**Note for 05:** the receipt needs a record of what a turn changed, so leave the
turn boundary something this store can describe.

## Answer

Built as the ratified *Where chat history lives* describes it, with no
re-decision of its four parts. A conversation is JSON under
`extension_path_user(__package__, path="conversations", create=True)`, scoped per
`.blend` path with alias-on-Save-As, capped at 200 messages / 1 MiB with
whole-turn pruning, never inside a `.blend`, and with deletion only through an
explicit confirmation. Both of that ticket's measured behaviours were re-measured
here rather than trusted (see §6).

### 1. What changed, and where

| File | What it now holds |
|---|---|
| `blender_copilot/store.py` (new, 549 lines, **no `bpy`**) | The whole storage rule: turn spans, the two caps and the whole-turn prune, `active.json`, one JSON file per conversation, `tmp`+`os.replace` writes, corrupt-index self-heal, alias-on-adopt, the guarded `delete_all`, and the header label |
| `blender_copilot/scope.py` (new, 261 lines, the `bpy` half) | The directory (with the `ValueError`/`""` guard), `switch`, `persist`, `clear`, `delete_all`, `reveal`, and the three `@bpy.app.handlers.persistent` handlers (`load_post`, `save_post`, `save_pre`) |
| `blender_copilot/conversation.py` | `messages_from_history()` (the rows re-derived from the wire), `snapshot()`, `restore()`, `abandon()`, and a generation fence in `apply_event` |
| `blender_copilot/stream.py` | The write point: `scope.persist()` on the tick a turn ends — the only moment history changes |
| `blender_copilot/panel.py` | The scope line in the header, the history box (scope, retention admission, `Show folder`, `Delete all`), two new operators, and `use_fake_user = False` on the mirrors |
| `blender_copilot/__init__.py` | Registers the new operators and calls `scope.start()` / `scope.stop()` |
| `tests/test_store.py` (new) | 75 checks for the store, on plain CPython |
| `tests/test_conversation.py` | +36 checks: the restore/rebuild and the mid-turn file switch |
| `tools/panel_draw_smoke.py` | +6 checks: the header scope line, the history box, the retention admission, the confirmation's existence |
| `tools/persistence_probe.py` (new) | The event-loop-free half of persistence, headless: 67 checks |
| `tools/persistence_gui_probe.py` (new) | The two GUI stages: chat-and-quit, then reopen-and-look. 9 + 10 checks plus three screenshots |

Three decisions inside the ticket, stated rather than buried:

- **The store keeps one schema: the wire one.** The display transcript is
  re-derived by `messages_from_history()` instead of being written beside it. The
  cost is real and named: a block that only ever explained something *live* — the
  transport banner, a cap's notice, the `[stopped]` marker — has no wire form and
  does not come back. What the model saw and answered does.
- **`save_pre` removes the panel's `Text` mirrors.** *Where chat history lives*
  left this a choice — strip them on save, or accept and label the leak. Stripped:
  a `Text` datablock with a user is written into the `.blend` and travels with it
  (measured in that ticket), and this ticket's fourth box says nothing of the
  conversation goes in the file. Visible consequence, stated for the human: a Text
  Editor displaying `Copilot Transcript` is emptied by a save.
- **`BLENDER_COPILOT_HISTORY_DIR`** overrides the directory. Not in the decision;
  added so a probe can run the real code against a real directory without writing
  probe conversations into the user's own config — the trick
  `tools/capability_probe.py` already uses with `BLENDER_USER_CONFIG`. The real
  directory is still computed by the product's own function and asserted, so the
  `__package__` expression is not left untested.

For **05**: the turn boundary is `store.turn_spans(history)` (and `store.turns()`),
already used by the pruner. The turn that just ended is the last span, and its
`tool` results are the ones whose `tool_call_id` matches a call in it — which is
what a receipt needs to say what changed. The push-at-end hook has a natural home
next to `scope.persist()` in `stream._tick`.

For **14**: the file carries `schema: 2` with `meta` beside the wire `messages` —
`meta.retention` is written by this ticket's prune
(`{turns_dropped, dropped_through, reason}`), and any other key under `meta`
(including `context_trim`) is round-tripped untouched, which is tested. The cap is
applied **at write time only**: the in-memory history is not trimmed mid-session,
because what the model sees is 14's projection and truncating the user's own
record on top of that would be two invisible forgettings instead of one visible
one. The retention sentence 14 §5 asked for exists — the pager it named for it no
longer exists, so it lives in the history box.

### 2. The gates

```
$ python3 tests/test_conversation.py
all checks passed                     # 203 ok, exit 0

$ python3 tests/test_store.py
all checks passed                     # 75 ok, exit 0

$ python3 tools/bounded_run.py 60 -- /Applications/Blender.app/Contents/MacOS/Blender \
      --background --factory-startup --python tools/panel_draw_smoke.py
ok   no history directory in this run, and the scope says so: 'unsaved — session only'
ok   a file with nowhere to save says `session only` too
ok   the header names the scope, so `session only` is visible before it matters
ok   the history box offers the folder and the delete
ok   a pruned store admits it on screen, with the count
ok   delete-all goes through a confirmation, not straight to execute
SMOKE OK
BOUNDED | finished on its own in 0.8s | rc=0
```

Gated on the token, not the status. The three commands were run chained with `&&`
(`CHAIN_RC=0`).

```
$ /Applications/Blender.app/Contents/MacOS/Blender --command extension validate
Success parsing TOML in "."
```

### 3. The persistence probe — the whole loop, headless (67 ok, `SMOKE OK`)

```
$ BLENDER_COPILOT_HISTORY_DIR=/tmp/bc-t04-hist/conversations python3 tools/bounded_run.py 120 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/persistence_probe.py

history dir (override): /tmp/bc-t04-hist/conversations
ok   the override is what the store uses
real extension user dir: /Users/user/Library/Application Support/Blender/5.2/extensions/.user/user_default/blender_copilot/conversations
ok   the real directory comes from the extension's own package name
bare package name raises: ValueError: The "package" does not name an extension
ok   a bare package name raises, which is why the guard is there
ok   load_post is registered / save_post is registered / save_pre is registered
ok   nothing is filed yet - an empty conversation is not worth a file
the turn took 4 ticks
ok   ending the turn wrote exactly one conversation file
ok   the stored history is the wire history, verbatim
ok   including the assistant's tool_calls and the tool result that answers it
ok   the sentinel prompt is on disk
ok   and the index points the file path at it
ok   the record carries the schema ticket 14 will extend
ok   the write is not repeated on a tick that changed nothing
ok   an unsaved file's conversation is not written
ok   while the panel says it is session-only
ok   the first save files the conversation it was carrying
ok   and the file it came from keeps its own
ok   save_pre removed the mirrors, so the .blend cannot contain them
ok   the sentinel is not in the saved .blend
ok   nor is the tool output
persist-a.blend is 525487 bytes
ok   Save As re-keyed the conversation rather than starting one
ok   and kept the old path as an alias
ok   no second conversation file appeared
ok   both names point at the same conversation
ok   opening another file switched the scope
ok   the conversation did not follow
load_post counters after one file open: {'decorated': 1, 'plain': 0}
ok   the decorated handler ran on the file load
ok   and the undecorated one did not - the decorator is what survives the handler flush a file read performs
ok   reopening the file restored the scope
ok   and the wire history with it
ok   with the user's ask, the model's prose, the code and the tool row
ok   the tool row came back finished, not running
300 stored messages became 198
ok   the cap pruned to its limit
ok   it starts at a turn boundary
ok   no result lost the call that produced it
ok   and no call lost its result
ok   the pruning is recorded, so it is not silent
ok   Clear ended the conversation without deleting it
ok   and started an empty one in the same scope
delete_all removed 2 file(s)
ok   delete-all removed every conversation / and the directory is gone
ok   wm.path_open exists for `Show folder`
ok   and there is nothing to reveal after delete-all
ok   a file whose history was deleted opens empty, without error
SMOKE OK
```

Two notes on what this probe is: the extension is imported as
`bl_ext.user_default.blender_copilot`, so `__package__` is the name
`extension_path_user` needs — the *real* directory is computed by the product's
own `scope._directory()` and printed, and only the writes are redirected. And the
turn is clocked by calling `stream._tick()` in a loop, because `bpy.app.timers`
never pump under `blender -b`; it is the shipped tick, driven by a script.

### 4. The GUI probe — quitting, reopening, and the confirmation

```
$ BLENDER_COPILOT_HISTORY_DIR=/tmp/bc-t04-gui/conversations python3 tools/bounded_run.py 90 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --python tools/persistence_gui_probe.py -- --stage 1
turn finished after 0.74s, status 'idle'
ok   the real event loop wrote the conversation on the tick the turn ended
ok   with the turn's wire messages
ok   the sentinel is on disk, so stage 2 has something to find
screenshot: persistence-stage1.png, sidebar tab 'Copilot'
SMOKE OK                                   # Blender quit itself, 2.6s

$ BLENDER_COPILOT_HISTORY_DIR=/tmp/bc-t04-gui/conversations python3 tools/bounded_run.py 90 -- \
    /Applications/Blender.app/Contents/MacOS/Blender --python tools/persistence_gui_probe.py -- --stage 2
ok   a new Blender starts with an empty conversation
ok   opening the saved file switched the scope to it
ok   and the conversation came back
restored 4 wire messages, 5 rows
ok   the rows were rebuilt from the wire
ok   the tool row is finished, not running
conversations before: ['40cbefdabcc9.json', 'active.json']
bpy.ops.blender_copilot.delete_history('INVOKE_DEFAULT') -> {'RUNNING_MODAL'}
ok   nothing was deleted while the confirmation is unanswered
SMOKE OK                                   # 1.9s
```

Stage 1 and stage 2 are **separate Blender processes**: the first chats and then
quits Blender itself, the second starts fresh and opens the same file. That is the
first box, measured rather than argued.

Screenshots (`logs/`, cited, and each says what it does not show):

- `logs/persistence-unsaved.png` — the header reads **`unsaved — session only`**
  and the history box says "This file is unsaved, so nothing is written to disk
  yet." It does not show the store: a screenshot cannot.
- `logs/persistence-stage1.png` — after one turn in a live session: the header
  scope line `persist-gui.blend`, the sentinel turn, a finished tool row with its
  output behind the expander, and the history box with `Show folder` / `Delete
  all` and "Kept per .blend file, outside the file itself."
- `logs/persistence-stage2.png` — the same transcript in a **fresh Blender**, which
  is the box above with a picture attached.
- `logs/persistence-confirm.png` — the modal: **"Delete all chat history?"**,
  "Every stored conversation is removed from disk. This cannot be undone.",
  Cancel / Delete. It shows the prompt; it cannot show the deletion that follows a
  click, so that is what the operator's own `execute` is for.

### 5. The two measured behaviours from the decision ticket, kept

- **`extension_path_user` raises, it does not only return `""`.** Re-measured with
  a bare package name: `ValueError: The "package" does not name an extension`. The
  guard catches the exception *and* the empty string, which is why every headless
  check in this repo (where the add-on is imported as `blender_copilot`) runs in
  the degraded, in-memory mode and says so on screen. That degradation is now
  itself a check in the smoke gate.
- **The `load_post` handler needs `@bpy.app.handlers.persistent`.** Re-measured
  as an experiment rather than asserted: two handlers registered side by side, one
  decorated, one not, then one file load — `{'decorated': 1, 'plain': 0}`. The
  undecorated one never ran.

### 6. What could not be verified

- **`Show folder` actually opening the OS file browser.** `wm.path_open` is
  confirmed to exist (its RNA docstring) and `reveal()` is confirmed to refuse when
  there is no folder. It was never called: it would have opened a Finder window
  during an unattended run.
- **A real button click on `Delete all`.** The prompt is photographed and
  `INVOKE_DEFAULT` returns `RUNNING_MODAL` with nothing deleted, but no click is
  synthesized. Worth knowing, because it cost a wrong first reading: a bare
  `bpy.ops.blender_copilot.delete_history()` returns `{'FINISHED'}` and deletes
  everything, since `bpy.ops` without `INVOKE_DEFAULT` goes straight to `execute`.
  That is Blender's operator-call semantics, not this code — but it means the
  confirmation is only load-bearing for the path a *user* takes, and a probe that
  calls the operator naively will not notice the difference.
- **Writing into the real extension user directory.** The real path is computed by
  the product's function and asserted to end in
  `/.user/user_default/blender_copilot/conversations`; every write in these runs
  went to `/tmp`. The same `makedirs` + write code runs either way, but the root
  differs, and that is the one thing a probe cannot fake.
- **A `.blend` that already contains a `Copilot Transcript` datablock.** The strip
  is verified for the current session; a file saved by an older build would still
  carry one until it is opened, re-saved, or the datablock removed by hand.
- **An interrupted `.blend` write.** `os.replace` is used and the JSON round trip
  is tested, but no torn-file case was staged.
- The GUI probes ran against the user's enabled extension, i.e. the *installed*
  `bl_ext.user_default.blender_copilot`, at the time of the run. The `--background`
  probe ran `--factory-startup`.
