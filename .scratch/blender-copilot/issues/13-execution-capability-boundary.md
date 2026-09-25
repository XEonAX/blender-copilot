# The capability boundary for model-authored code

Type: grilling
Status: open
Blocked by: none

## Question

*What replaces undo as the recovery mechanism?* concluded that no recovery
mechanism can cover arbitrary code, and answered by proposing that auto-run
survives only for a **curated capability**: a `run_blender_python` namespace that
gives `bpy`/`mathutils`/compute stdlib and withholds `open`, `__import__` outside
an allowlist, filesystem/process/network modules, `bpy.app.timers`/handlers, the
filesystem-touching `bpy.ops` families (`wm`, `script`, `export_*`/`import_*`,
`image.save*`) and `Text.write()`/`Image.save` methods — with full power behind an
explicit, default-off user gate. Its own words: **blast-radius reduction, not a
sandbox.**

That proposal is a sentence, not a specification. This ticket turns it into one,
and tries to break it.

Decide:

1. **The exact allowlist**, as a data structure, and the exact mechanism that
   installs it (a restricted `__builtins__`, an import hook, a `find_spec` guard,
   module proxies — pick, and say what each does not cover).
2. **The `bpy.ops` curation.** Withholding `bpy.ops.wm.*` etc. is a name-level
   claim; `bpy.ops` is a namespace object, `getattr` is dynamic, and
   `bpy.data`/RNA is a wide side door. Enumerate the direct and indirect paths to
   each withheld capability and say which are actually closed, which are
   best-effort, and which are open. `bpy.ops.script.python_exec`,
   `bpy.ops.wm.save_as_mainfile`, `Text.write`, `Image.save`, `bpy.data.libraries`,
   `bpy.utils.execfile`, `importlib`, `__loader__`, `sys.modules` rewriting,
   `ctypes`-free subprocess alternatives, and `bpy.app.handlers` are all in scope;
   find the ones not on that list too.
3. **The adversarial probe.** Write it, run it headlessly, and record the escapes
   it finds. A probe that finds nothing is evidence only if it tried the obvious
   bypasses hard — say which ones it tried and which it did not.
4. **The gate.** Where the full-power path is granted, what it is called in the
   UI, whether it is per-run or per-session, what it re-verifies afterwards, and
   what the transcript records about which mode ran.
5. **The failure mode of the guard itself.** What happens when the allowlist
   breaks legitimate work (a bound-method check, a library that imports
   `subprocess` at module scope, a `bpy` call that internally touches the
   filesystem). Predictable breakage is worth naming before it is built: does the
   guard fail closed with a clear tool error, and is there a documented escape
   hatch that does not require a new session?
6. **Cacheability.** The system prompt's `{capability}` line is generated from
   the same constant that builds the namespace (per *What the prompt teaches the
   model about Blender*). Fix the constant's home so the prompt, the namespace and
   the ticket 06 `tool_argument_error` text cannot drift apart.

Read *What replaces undo as the recovery mechanism?* §6 and *What the prompt
teaches the model about Blender*'s Surface 1 first — they own the proposal and the
wording contract.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
