# What exactly happens when we exec model code, and what does undo cover?

Type: research
Status: resolved
Blocked by: none

## Question

When the addon executes model-authored Python in a *live* Blender 5.2 session, what precisely happens — and what does the recovery mechanism actually cover? This holds the highest-stakes answer on the map, because the design auto-runs arbitrary code with no approval gate and leans on undo as the backstop.

Answer at least:

1. **Execution.** The supported way to `exec()` a code string from inside an addon and capture **stdout, stderr, and the traceback** (`contextlib.redirect_stdout`? what does the Python Console do internally? what does `bpy.ops.script.python_file_run` do?). How should output be captured so the model can read it back?
2. **Operator failures.** How do `bpy.ops` failures surface to Python — poll failure text, exceptions, or silent `{'CANCELLED'}`? Can an addon report *why* a poll failed (cf. `poll_message_set`)? Are these catchable by the caller?
3. **What is undoable.** Precisely: direct RNA/data writes (e.g. `obj.location = ...`), vs operators with `bl_options = {'REGISTER','UNDO'}`, vs operations with no undo flag. Does a mixture inside one `exec()` produce one step, several, or an inconsistent stack?
4. **`bpy.ops.ed.undo_push`.** Its poll requirements, what it does when there is no undo stack, and whether it can be called safely from a timer callback (i.e. with a minimal context). Also `bpy.ops.ed.undo` / `redo` and whether the stack survives script errors.
5. **The snapshot.** Is `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)` a safe and cheap way to snapshot the current file before a turn's first mutation? Its cost on a large scene, whether it disturbs the user's file path/dirty state, and whether a lighter mechanism exists. This is our crash-recovery backstop — the alternative to an approval gate.
6. **A covers / does-not-cover table for undo.** Explicitly including what undo can never reach: filesystem writes and deletes, subprocesses, network calls, external assets.
7. **Network permission gating.** How `bpy.app.online_access` behaves in 5.2, how an extension's `[permissions] network` interacts with it, what a network call does when it is off, and the correct way for the addon to check before calling the API.
8. **Preferences storage.** Where addon preference values physically persist on macOS for Blender 5.2, and whether they are stored encrypted or **plaintext**. (Relevant because we may store an API key there — a sibling ticket decides whether we do.)

Facts already established (do not re-derive): Blender 5.2.2 ships Python 3.13.13; `requests` and `certifi` are bundled; TLS to `api.openai.com` works from Blender's Python.

## Answer

This ticket reopened the map's recovery design. Read all of it before trusting undo with anything.

- **Python-initiated operations do not push undo.** `WM_operator_call_py()` bumps `wm->op_undo_depth` around every operator call made from Python — from `exec`, from a timer, or from the Console — so such calls neither create an undo step nor register for redo. Direct RNA writes push nothing either; they are only captured by a *later* snapshot.
- **Therefore a "per-call undo step" is not automatic.** It requires an explicit `bpy.ops.ed.undo_push`, and (per the researcher's own correction during testing) the push belongs at the **end** of the unit you want to revert — pushing *before* a turn makes Ctrl+Z revert too far.
- **Undo covers:** local `bpy.data` only, by whole-file snapshot, and only after an explicit push; deletions, if a step predates them; selection/mode, as part of a snapshot.
- **Undo never covers:** filesystem writes and deletes; subprocesses; network calls; render results and GPU state; screens/workspaces/WM/brushes; addon preferences and `userpref.blend`; Python state (globals, `sys.modules`, registered handlers); linked-library data; and **nothing at all when Global Undo is off**. The stack is also cleared by `open_mainfile` / `read_factory_settings`.
- **The network permission is a declaration, not a sandbox.** Verified independently while resolving this ticket: with `--offline-mode` set and `bpy.app.online_access == False`, an HTTPS request from Blender's Python still reached `api.openai.com` (HTTP 401). `online_access` gates Blender's *own* features; it does not stop Python sockets. Any auto-run script can reach the network regardless of the manifest.
- **Addon preferences are plaintext.** They persist into the user preference file, a `.blend` with no encryption — which is what the API-key ticket has to weigh.

Detail and citations: `research/blender-code-execution-undo.md`. Consequence: new ticket *What replaces undo as the recovery mechanism?*

**Re-confirm before relying on it:** the undo behaviour was tested in a GUI session by the researcher, and undo needs a screen — it cannot be checked under `blender -b`. Verify early in the panel prototype.

## Comments
