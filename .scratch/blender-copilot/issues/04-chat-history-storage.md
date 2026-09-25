# Where chat history lives

Type: grilling
Status: resolved
Blocked by: none

## Question

Where does a conversation live between Blender sessions, and what is a "conversation" here?

Decide all of:

1. **Storage location.** JSON under Blender's user config / extension preferences directory (survives restart, per-user, does not pollute any `.blend`); inside the `.blend` as a Text datablock or custom property (travels with the file, but bloats it and leaks your prompts to whoever receives it); or in-memory only (lost on restart).
2. **Scoping.** Per-user, per-file (keyed by `.blend` path), or per-project directory? What happens when the user switches `.blend` files mid-conversation — resume the thread, start a new one, or refuse until they choose?
3. **What a conversation is.** One rolling stream per session, or named threads the user can start and return to?
4. **Retention.** How is history cleared, how is it capped, and does the user get an obvious way to delete it?

Weigh the fact that this is a *local developer tool* against the risk that a `.blend` containing a transcript is a distribution hazard.

## Answer

**RATIFIED 2026-09-25 by the project owner — accepted as written.** No longer
provisional, including the per-`.blend`-path scoping the ticket called its most
contestable choice. The record is [docs/ratification.md](../../../docs/ratification.md).

### Verified against the installed Blender 5.2.2 (background `-b`, no GUI launched)

- `bpy.utils.extension_path_user(package, *, path='', create=False)` exists; `path` is keyword-only. With
  `__package__` it returns
  `~/Library/Application Support/Blender/5.2/extensions/.user/user_default/blender_copilot/<path>` and
  `create=True` does `os.makedirs`. It returns `""` on failure, not `None`. Source with docstring:
  `/Applications/Blender.app/Contents/Resources/5.2/scripts/modules/bpy/utils/__init__.py:952-995`;
  docstring says the **extension's own directory is cleared on every upgrade**, which is why this API exists.
- A `Text` datablock **with a user** is written into the `.blend`: +38,478 bytes for a 50 KB
  incompressible payload. With `use_fake_user = False` (users 0) it is **dropped on load** (0-byte delta).
  `bpy.data.texts.new()` defaults `use_fake_user=True`. So an embedded transcript both persists and
  travels.
- `bpy.data.filepath` is `''` for an unsaved file and the new path at `load_post`. Verified handler
  sequence for open/save/homefile.
- **Trap:** a `load_post` handler without `@bpy.app.handlers.persistent` is removed *before* it can run on
  the first file load — it never fires. With the decorator it survives and `load_post` sees the new path.
- The confirm-dialog idiom in 5.2.2 is `context.window_manager.invoke_confirm(self, event, title=...,
  confirm_text=...)` (bundled use: `5.2/scripts/startup/bl_operators/presets.py:731`);
  `bpy.types.Operator` itself has no `invoke_*` helpers (`_bpy_types` has none).
- `bpy.ops.wm.path_open(filepath=...)` exists ("Open a path in a file browser").
- stdlib `json` is importable from Blender's Python; no wheel or manifest permission is needed to write
  local files.

### 1. Storage location — JSON under the per-extension user dir. Never in the `.blend`.

`bpy.utils.extension_path_user(__package__, path="conversations", create=True)`; guard against the `""`
return by degrading to in-memory only and saying so in the panel header. One file per conversation, plus a
small `active.json`. Writes are `tmp` + `os.replace`.

Rejected:

- **`Text` datablock in the `.blend`.** Verified it persists, and that is the disqualifier: a `.blend` is
  the artifact that gets mailed, packed into a bug report, or shipped with a scene. "Local dev tool"
  explains why per-user storage is cheap; it does **not** license leaking prompts to whoever receives the
  file. Embedded history also cannot be deleted without editing the file.
- **Addon preferences** (`userpref.blend`, verified to exist at `.../5.2/config/userpref.blend`). Global
  across all files, so per-file scoping becomes impossible, and chat mixes with settings. Overlaps
  *Where the API key lives* but loses on scoping alone.
- **In-memory only.** Loses the thread on every restart, every crash, and every `.blend` switch — and with
  no scroll in the panel, re-reading old turns is exactly what history is for.

### 2. Scoping — per `.blend` file path, with path aliasing.

- `active.json` maps absolute `bpy.data.filepath` → conversation id; each conversation file records its own
  `scope.blend_path` so a corrupt index self-heals by scanning headers.
- **Switch mid-conversation (`load_post`, decorated `@bpy.app.handlers.persistent`):** do not carry the
  thread. Bump a generation counter so late chunks from the transport worker for the old file are dropped,
  flush the old conversation, load the new file's conversation or start one, and let the panel **header**
  show the new basename. No fake "you switched files" message is written into the history — the store is
  the provider wire format and must not accumulate UI-only lines.
- **Unsaved file (`filepath == ''`):** session-only, not persisted, header reads "unsaved — session only".
  On the first `save_post` that gives it a path, the active conversation adopts that scope and is written.
- **Save As / rename:** re-key the active conversation to the new path and keep the old path as an alias,
  so both locations find the same file.

Rejected: refusing until the user chooses (friction, and a panel has no modal-free way to ask); carrying the
thread across files; and a single global stream. All three let history from scene A describe objects that
no longer exist in scene B — precisely the stale-reference failure *The agent loop's control flow and
failure policy* makes a top trust rule.

### 3. What a conversation is — one rolling conversation per scope, one file per conversation.

Named threads with a picker are **deferred**, not rejected: the on-disk layout (one conversation per file,
`active.json` pointing at one) already supports a picker as a purely additive UI change later. Building the
picker now spends ticket 08's entire affordance budget on a UI the panel cannot render well (no scroll, no
rich text).

Messages are stored **verbatim in the provider's wire format** (`role`, `content`, `tool_calls`, `tool`
results) so restoring a conversation requires no translation and no lossy second schema. Rejected: a single
ever-growing per-user stream (unbounded size, one corrupt file loses everything, no selective delete).

### 4. Retention

- **Cap:** 200 messages or 1 MiB serialized, whichever first. Prune whole turns from the front. Invariant:
  never orphan a `tool` result from its parent `tool_calls` (the provider rejects it) — flagged as a
  provider-contract assumption to confirm when the loop is built, not asserted as verified here.
- **Clear means finalize, not delete.** The panel's trash action ends the current conversation and starts a
  fresh one; nothing is destroyed implicitly. Deletion is explicit: a "Delete all chat history" action using
  `window_manager.invoke_confirm` (verified idiom) that removes the whole `conversations/` directory.
- **Obvious delete path:** a button calling `bpy.ops.wm.path_open(filepath=<dir>)` to reveal the folder.
- Button placement belongs to *How a conversation is laid out and controlled*, not here.
- Compaction-degradation policy is **not** decided here; it is the map's *How a conversation degrades as
  context grows* frog.

### Handoff to *How a conversation is laid out and controlled*

The research offers a `Text` Editor bound to an addon-owned `Text` datablock as the no-scroll workaround for
full history. That datablock is a **second leak vector** and it is the token *inside a displayed editor*
that makes it persist. If ticket 08 picks it, it must either be stripped in a `save_pre` handler or the leak
must be accepted and labelled; the authoritative transcript remains the JSON file.

### What a human must ratify

Ratify that chat history is stored as JSON under
`extension_path_user(..., "conversations")`, scoped per `.blend` path with alias-on-Save-As, capped at
200 messages / 1 MiB with front-turn pruning, never embedded in a `.blend`, and with deletion only via an
explicit confirmed "delete all" plus reveal-in-folder — or overrule the per-file scoping (the most
contestable choice: a multi-`.blend` project fragments history across files) and the deferred named threads.

## Comments
