# The capability boundary for model-authored code

Type: grilling
Status: resolved
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

**PROVISIONAL — no human present.** Every decision below is mine; the
ratification list at the end is what a human must sign.

Probe: [`tools/capability_probe.py`](../../../tools/capability_probe.py), run as
`BLENDER_USER_CONFIG=/tmp/bc-t13-cfg /Applications/Blender.app/Contents/MacOS/Blender
--factory-startup -b -P tools/capability_probe.py` against the installed 5.2.2
(Python 3.13.13). It contains a *reference implementation* of the spec below, so
"closed" means "this exact mechanism raised on it", not "a sentence says so".
Raw results: 77 attempts → **49 denied, 2 ESCAPED, 11 blocked by absence/type,
15 allowed**; 9 baseline canaries all fired with the guard removed except one
(false premise, §2).

### 0. Corrected premises (all measured)

- **`bpy.ops.script.python_exec` does not exist in 5.2.2.** `bpy.ops.script` is
  `execute_preset | python_file_run | reload`. The ticket's premise and ticket
  12's list should say `python_file_run`.
- **`Text.write()` is not a file capability.** Its RNA description is *"write
  text at the cursor location and advance to the end of the text block"* — it
  edits the buffer. `write(text=...)` on an in-memory or loaded Text returned
  `None` and left the file untouched. Text-to-disk is `bpy.ops.text.save` /
  `save_as`, already denied with the `text` family. Ticket 12's "`Text.write()`"
  item is wrong; drop it and keep `Image.save` (RNA: "Save image", params
  `filepath, quality, save_copy`), which is real and writes.
- **`hasattr(bpy.types.Image, "save")` is `False`** even though instances have
  it. The house rule holds: use `bl_rna.functions` or an instance. Both
  `Image.save` and `Text.write` appear in `bl_rna.functions`.
- **An allowlisted compute module can carry the whole escape.**
  `import numpy; numpy.save('/tmp/x.npy', ...)` wrote a file in run 1.
  `statistics.sys`, `random._os`, `uuid.os`, `enum.sys`, `dataclasses.inspect`
  are all in those modules' `__dict__` — so raw module objects are themselves a
  hole and must be proxied (§1).

### 1. The allowlist and the installing mechanism

Home: `blender_copilot/capability.py`, module-level frozen data only.

```
ALLOWED_BUILTINS      explicit set (not builtins.__dict__ minus a blacklist)
ALLOWED_MODULES       math cmath statistics random itertools functools collections
                      heapq bisect array decimal fractions numbers re string
                      textwrap unicodedata json dataclasses enum typing abc copy
                      operator uuid datetime time hashlib base64 struct zlib csv
                      mathutils numpy
MODULE_DENY           sys os _os inspect ctypes ctypeslib builtins importlib gc
                      subprocess socket io _io pathlib shutil tempfile threading
                      posix signal select resource mmap pickle marshal code codeop
                      runpy webbrowser urllib http ssl traceback linecache jit
                      distutils setuptools  +  numpy's file surface: save savez
                      savez_compressed load fromfile memmap loadtxt savetxt
                      genfromtxt DataSource f2py testing lib core
ALLOWED_BPY_ATTRS     data context types props path ops app
DENIED_OPS_FAMILIES   wm script text text_editor image render file preferences
                      extensions console ed outliner export_scene import_scene
                      export_anim import_anim import_curve export_mesh import_mesh
                      sound clip asset pack screenshot grease_pencil
DENIED_CONTEXT_ATTRS  preferences window_manager window screen area region
                      space_data workspace file asset_library_ref
DENIED_APP_ATTRS      timers handlers driver_namespace binary_path
DENIED_DATA_ATTRS     libraries
DENIED_COLLECTION_*   images.load/save/..., texts.load/open, sounds/fonts/
                      movieclips/volumes/cachefiles/brushes.load
DENIED_DATABLOCK_*    images.save/save_render*/pack/unpack
```

Installed by four layers, each of which names what it does **not** cover:

1. **Explicit `__builtins__` dict.** Closes `open`, `input`, `eval`, `exec`,
   `compile`, `globals`, `locals`, `breakpoint`, and an unguarded `__import__`
   (all measured: `NameError`/refused). Does **not** cover attribute
   introspection — `().__class__`, `type()`, `getattr`, `__build_class__` stay,
   because class definition and normal code need them, and removing them would
   not help anyway (see §3).
2. **Namespace `__import__` = `guarded_import`.** Checks the top-level name
   against `ALLOWED_MODULES`, then returns a **module proxy**, never the raw
   module. Closes `import os|sys|ctypes|subprocess|socket|importlib|io|pathlib|
   gc|inspect`, `from os import system`, `__import__('os')`, and the
   `statistics.sys` / `random._os` / `uuid.os` dictionary leaks. Does **not**
   cover a public attribute of an allowed module that is itself dangerous
   (`numpy.save` before `MODULE_DENY`; closed now, by name).
3. **Closed proxies for `bpy` and its submodules.** `bpy` itself is a proxy, so
   `bpy.utils`, `bpy.msgbus` and `bpy.__dict__` are not reachable; `bpy.data`,
   `bpy.context`, `bpy.app`, `bpy.path`, `bpy.props`, `bpy.types` are
   sub-proxies. `bpy.ops` is a family proxy; `bpy.data.images`/`texts` are
   collection proxies whose items are datablock proxies. Does **not** cover
   anything reached through a raw scene datablock's object graph, and the
   closure/globals leak in §3.
4. **No `sys.meta_path` finder.** Measured: an allowlist-based finder fails
   closed on `numpy`'s own `import contextvars` (legitimate work broken), and it
   closes nothing layer 2 does not — an allowed module's internal imports use
   that module's own real `__builtins__`, so the finder cannot stop them either.
   A deny-list finder would catch fresh imports of `os` but adds a global,
   process-wide interposer for no measured gain. Rejected.

### 2. `bpy.ops` curation, and the RNA side door

| Path | Verdict | How |
|---|---|---|
| `bpy.ops.wm.*` (save/open/link/append/quit/url_open/path_open) | **closed** | family proxy, `getattr(bpy.ops.wm, name)` included |
| `bpy.ops.script.*`, `bpy.ops.text.*` (`run_script`, `python_file_run`, `text.save`) | **closed** | family proxy |
| `bpy.ops.image.save_as`, `export_scene.*`, `import_*` | **closed** | family proxy |
| `bpy.ops.ed.undo*` (loop owns them) | **closed** | family proxy |
| `bpy.ops.__dict__`, `bpy.__dict__`, `bpy.utils.*` (incl. `execfile`) | **closed** | not in `ALLOWED_BPY_ATTRS` |
| `bpy.app.timers` / `handlers` / `driver_namespace` | **closed** | attribute denied |
| `bpy.context.preferences` (the API key, global undo) incl. inside `temp_override()` | **closed** | context proxy + wrapper (measured) |
| `bpy.data.libraries.write/load` | **closed** | collection denied |
| `bpy.data.images.load`, `bpy.data.texts.load` | **closed** | collection method denied |
| `D.images[...].save`, `D.images.new(...).save` | **closed** | datablock proxy |
| `Image.save` through an indirect RNA slot (`ShaderNodeTexImage.image`) | **best-effort** | closed only because RNA rejects the proxy type (`expected a Image type, not _P`); it also *breaks* the legitimate `node.image = D.images.new(...)` |
| `numpy.save` / `fromfile` / `loadtxt` / `memmap` | **best-effort** | name deny. Escaped in probe run 1; closed in run 2. `numpy.lib.npyio` is closed by denying `lib`/`core` at the module level, not by enumerating functions |
| module `__dict__` (`bpy.path._os`, `bpy.props.__dict__`) | **closed** | module proxy denies `_`-prefixed and `MODULE_DENY` |
| raw scene datablocks (`D.objects`, `D.meshes`, `D.collections`, `D.screens`) | **open by construction** | needed for the work; they carry `__class__`/`__init__.__globals__` |
| `sys.modules` rewriting, `__loader__`, `importlib` | **closed** | no `sys`, no `importlib`; module proxies deny `_`-prefixed |
| `ctypes`-free process spawn (`os.system`, `posix.spawn*`, `socket`) | **closed** by import; **open** by introspection | see §3 |

### 3. The adversarial probe: what it found

Baseline (guard off) confirmed all nine canaries are real: `open`, `subprocess`,
`bpy.data.libraries.write`, `Image.save`, `wm.save_as_mainfile`,
`handlers.append`, `timers.register`, `preferences.edit.use_global_undo`.
(`Text.write` was the false one, §0.)

Under the guard, 49 of 77 attempts were denied and every legitimate path still
worked: `math`, `statistics`, `numpy`, `dataclasses`, `enum`,
`bpy.ops.mesh.primitive_cube_add()` and its `get_rna_type()`, `D.objects['Cube']
.location.z = 1`, iteration over `D.images`, `bpy.context.scene`,
`temp_override(scene=...)`, `bpy.app.version_string`.

**Two escapes, both generic CPython introspection, neither importing anything:**

1. `().__class__.__bases__[0].__subclasses__()` → any class whose
   `__init__.__globals__` holds `sys` (measured hit: `_WeakValueDictionary`) →
   `sys.modules['os'].system(...)`. Canary file written. 970 subclasses.
   `FileIO` itself is not a *direct* subclass of `object`, so `sys` is the pivot.
2. `type(bpy).__getattr__.__globals__['real_builtins'].__import__('os')` — the
   proxy's own closure/globals leaks the unguarded builtins and the real `bpy`.
   Canary written.

These are **not closable at the Python level.** `__class__` is an attribute, not
in `__builtins__`; removing `object`/`type`/`getattr` would break class
definition and still leave `x.__class__`. A closure instead of an attribute does
not help: `type(p).__getattr__.__globals__` / `.__closure__` reach the cell.
Only a real isolation boundary (separate process, OS sandbox) closes them, and
both are outside this slice (live `bpy.context`, no IPC — map's standing
decision).

**Not tried** (so the probe is evidence, not proof): `gc.get_objects`,
`sys.settrace` / `sys.monitoring`, `_ctypes` buffer allocation, C-extension
symbol probing, dangling-RNA-after-undo, prompt-injection payloads derived from
scene text, `numpy.lib.npyio` aliases, `bpy.ops.text.save` under a real GUI Text
Editor context, and **anything in Full-access mode**. No GUI was launched; the
whole probe is `-b`.

### 4. The gate

- **Name and shape:** one mode chip at the top of the panel, **`Full access`**,
  default **off**, sub-line *"files, processes, network, preferences — not
  covered by Ctrl+Z"*. Scope: **per session** (process lifetime), not per run
  and not a global preference. Per-run exists only as the escape hatch on a
  denial row: `[Allow once] [Full access for this session]`. There is no
  per-call approval prompt, because the capability boundary is where a user can
  reason about it and a prompt per call is the thing ticket 12 already killed.
- **On denial**, the `run_blender_python` row shows
  `capability_denied: bpy.ops.wm` (or whichever name) with those two buttons.
  The model never sees the buttons; it sees the tool error and says so.
- **What it re-verifies afterwards:** the loop records `bpy.data.filepath`
  before and after the run, pushes the turn's undo step as usual (Full access
  does not change push discipline), and the receipt says *"ran in Full access;
  changes outside this .blend are not tracked or undoable"*. It does **not**
  claim to audit what the script did — it cannot.
- **What the transcript records:** every `run_blender_python` row stores
  `mode: scoped | full`, persisted in the conversation JSON (ticket 04) and
  mirrored to the addon-owned `Text` datablock (ticket 08) so the mode survives
  the history. A mode change writes its own transcript entry. The `{capability}`
  system-prompt line is regenerated from the constant when the mode changes, so
  the model knows what it has.

### 5. Failure mode of the guard

Fail closed, with a machine-readable tool error. `error.kind =
"capability_denied"`, `error.capability = "bpy.ops.wm"`, `error.hint` naming
whether a Full-access grant would help. This is an **amendment to ticket 06's
`kind` enum** (`exec_error | tool_argument_error | not_found | invalid_kind`).
`CapabilityDenied` should subclass **`AttributeError`** so `hasattr` /
`getattr(..., default)` probing behaves, and the loop classifies on `.capability`.

Named breakages, predicted before they are built:

- **RNA assignment of a proxied datablock** — `node.image = D.images.new(...)`
  raises `TypeError: expected a Image type, not _P`. This is a real scene
  operation the guard breaks. Mitigation: the tool rewrites a `not _P`
  `TypeError` into a clear `capability_denied`-style message naming the proxy,
  and the tool description says so. Do **not** silently unwrap.
- **Transitive imports** — the reason the `meta_path` finder is gone. Any module
  an allowed module needs must be reachable; `numpy`→`contextvars` proved it.
- **`while True:` / a blocking C call** — the guard does not and cannot time
  bound them (ticket 12's `sys.monitoring` budget is unbuilt). The UI must keep
  saying a hang is force-quit-only.
- **Documented escape hatch with no new session:** the two denial buttons
  (once / this session) plus a startup preference `full_access_on_start`
  (default off). The loop's own code is **never** guarded — it uses the real
  `bpy`, so the undo push, receipt and recovery copy keep working in either
  mode.

### 6. Cacheability: one constant, three consumers

`blender_copilot/capability.py` owns the data in §1 **and**
`describe_capability(mode) -> str` for the `{capability}` line, and
`capability_error(capability) -> dict` for the tool error text. Consumers:

- the namespace builder (`build_namespace()`) reads the same sets;
- ticket 10's system prompt substitutes `describe_capability(mode)` — no
  hand-written restriction anywhere else;
- ticket 06's denial path calls `capability_error(...)`.

Anti-drift test (plain CPython, `tests/`): every name in `DENIED_OPS_FAMILIES`
and `DENIED_*_ATTRS` is provably denied by the built proxy; every name in
`ALLOWED_BPY_ATTRS` resolves; `describe_capability`'s text mentions every denied
family's human name; and a grep test asserts no capability sentence exists
outside `capability.py`. Prompt prefix stays **byte-stable per mode**, so the
provider's cache prefix holds across turns; toggling the mode legitimately
changes the prefix and misses the cache once — accepted, because a mode flip is
an explicit user act.

### 7. The uncomfortable conclusion, stated plainly

The guard is **hygiene, not containment** — ticket 12 already said "blast-radius
reduction, not a sandbox", and the probe puts numbers on it: a two-line payload
with no imports escapes, and the guard's own proxies leak their guts. So the
claim in ticket 12 §6 — *"the promise in §5 becomes true rather than
decorative"* — is **too strong and must be amended**: it is true for the model's
spontaneous mistakes and for every direct named path (which is most of what
actually happens), and false against a payload that intends to leave. The
Full-access gate is therefore a **consent surface, not a containment boundary**.
Rejected alternatives: a process-isolated executor (conflicts with live
`bpy.context` and the map's no-IPC decision), an OS sandbox (platform-specific,
outside the slice), AST/regex filtering (ticket 06 already rejected it; string
building defeats it; the probe adds that the direct-name paths are the ones
worth closing), and stripping `object`/`type`/`getattr` from builtins (breaks
class definition and does not close `x.__class__`).

### What a human must ratify

1. The contents of the §1 constant: `ALLOWED_MODULES` (**including `numpy`**,
   which needed its own file-function deny), `MODULE_DENY`,
   `DENIED_OPS_FAMILIES`, and the context/app/data deny sets.
2. Dropping the `sys.meta_path` finder, on the measured `numpy`/`contextvars`
   false-positive.
3. The corrections to ticket 12's capability list: drop `Text.write()`; keep
   `Image.save`; `bpy.ops.script.python_exec` does not exist in 5.2.2
   (`python_file_run` does).
4. Adding `capability_denied` to ticket 06's `error.kind` enum, and
   `CapabilityDenied` subclassing `AttributeError`.
5. The gate's exact UX: the name **`Full access`**, per-session scope, the
   two-button denial row, and the receipt sentence in §4.
6. The accepted breakage in §5 (`node.image = D.images.new(...)` and any RNA
   assignment of a proxied Image/Text), and the rewritten error message.
7. The constant's home at `blender_copilot/capability.py` plus the anti-drift
   test.
8. **The largest:** accepting the guard as hygiene rather than containment, and
   amending the map's provisional "auto-runs scene-only Python" clause so it
   reads *enforced for the direct paths, explicitly not a sandbox*, with the
   Full-access gate named a consent surface.

## Comments
