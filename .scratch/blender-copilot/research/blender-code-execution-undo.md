# Executing model-authored Python in a live Blender 5.2 session — and what undo actually covers

Research question: when an addon `exec()`s model-authored Python in a live Blender 5.2 session, what
exactly happens, and what does undo actually cover? This matters because the design auto-runs
arbitrary code with **no approval gate** and treats undo as the safety net.

**Evidence rules used here.** Every claim is cited to one of:

- `path:line-line` — the local source clone at `/Users/user/Projects/blender`, which is
  **5.3.0-alpha** (`source/blender/blenkernel/BKE_blender_version.h:23` `BLENDER_VERSION 503`,
  `:27` `BLENDER_VERSION_CYCLE alpha`, `:33` `BLENDER_FILE_SUBVERSION 24`). It is reference only; it
  was read, never modified. Line numbers are from that clone.
- A URL to official Blender documentation.
- `[verified 5.2.2]` + the exact probe, for things I actually ran against the installed
  **Blender 5.2.2 LTS** at `/Applications/Blender.app`.

Where a claim comes from source but was *not* executed, it is labelled **source-only**. Where
5.2 and 5.3 could plausibly differ, the runtime-verified claim (5.2.2) wins for this project.

Probe scripts live in `/tmp/bc-research/` (throwaway, outside the repo). Every probe ran with
either `--factory-startup -b` (background) or `--factory-startup` with a timer (real GUI), and with
`BLENDER_USER_CONFIG` pointed at a temp dir wherever a probe could have written preferences, so the
user's real `~/Library/Application Support/Blender/5.2` was never modified.

---

## Bottom line

`bpy.ops.*` calls made from Python — from `exec`, a timer, or the console — **never create an undo
step and are never registered for redo**, because `WM_operator_call_py()` bumps
`wm->op_undo_depth` around every Python-initiated call (verified: an operator-deleted object could
not be restored by `bpy.ops.ed.undo()`). Direct RNA writes (`obj.location = ...`) also push nothing;
they are only captured as a side effect of some *later* snapshot, which means a change made after
the newest push is **destroyed by the next undo and is not recoverable by redo** (verified). Undo
reaches exactly one thing: the local `bpy.data` half of the `Main` database. It can **never** reach
the filesystem, subprocesses, network calls, render results, the UI/workspace/preferences, or Python
state — and `bpy.app.online_access` does not block sockets at all (verified: HTTPS succeeded with
`--offline-mode`). Adopting undo as the safety net is therefore adopting an in-RAM, best-effort,
whole-file-snapshot mechanism that silently loses the model's un-pushed work and cannot see anything
the code did outside Blender's data.

---

## 1. The supported way to `exec()` a code string and capture stdout, stderr and the traceback

### 1a. What the Python Console does internally (the reference implementation)

The Python Console does **not** use any special bpy API. It is pure stdlib, in
`scripts/modules/_console_python.py`:

- A *persistent* interpreter per console: `code.InteractiveConsole(locals=namespace,
  filename="<blender_console>")` — `_console_python.py:117-118`. The namespace is a real
  `types.ModuleType("__main__")` with `__builtins__`, `bpy`, `C = bpy.context`, `D = bpy.data`, a
  replacement `help()`, plus `from mathutils import *` / `from math import *` pushed through the
  console itself so it lands in the same namespace — `_console_python.py:99-115`.
- Consoles are cached per region hash in a function attribute, so state persists across lines but is
  cleared when the window manager changes (i.e. a new file) — `_console_python.py:68-113`. The
  comment at `:76-78` is worth knowing: *"bpy.data hashing is reset by undo so can't be used"* — the
  console already had to work around undo invalidating its state key.
- Output capture on every execution — `_console_python.py:155-165`:

  ```text
  with (redirect_stdout(stdout),
        redirect_stderr(stderr),
        redirect_stdin(None),           # "Don't allow the stdin to be used because it can lock Blender."
        _TempModuleOverride("__main__", console._bpy_main_mod)):
  ```

  `stdout`/`stderr` are `io.StringIO()` objects recreated per execution — `_console_python.py:83-88`.
- The exec itself is `console.push(line_exec)` — `_console_python.py:176`. `SystemExit` is caught
  specially (`:178-183`), any other exception is formatted into the stderr buffer with
  `traceback.format_exc()` (`:184-186`).
- Afterwards: read both buffers, **clear them** (`truncate(0)`, `:194-197`), set
  `sys.last_traceback = None` (`:192`), and append the text to the console scrollback as
  `OUTPUT` / `ERROR` lines (`:224-227`).

The console operator wrapper is a *Python* operator, `ConsoleExec` — `scripts/startup/bl_operators/console.py:24-42`
— with `bl_idname = "console.execute"`, **`bl_options = {'UNDO_GROUPED'}`** (`:28`) and
`poll() = context.area and context.area.type == 'CONSOLE'` (`:34-36`). `'UNDO_GROUPED'` maps to
`OPTYPE_UNDO_GROUPED` (`rna_wm.cc:549-575`, `WM_types.hh:215`), which means running the console from
the Enter key *does* produce grouped undo steps, because a keymap invocation has
`op_undo_depth == 0`. **The console is therefore not a model for the addon's undo behaviour** — see §3.

### 1b. What `bpy.ops.script.python_file_run` does

- Operator definition: `source/blender/editors/space_script/script_edit.cc:53-67`; exec callback
  `run_pyfile_exec` at `:31-50`. Flags: `ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO | OPTYPE_INTERNAL;`
  (`:66`). Property: `filepath` (`:68-70`).
- It calls `BPY_run_filepath(C, filepath, op->reports)` (`script_edit.cc:37`) →
  `python_script_exec()` in `source/blender/python/intern/bpy_interface_run.cc:134-241`.
- Key properties of that execution:
  - **Fresh namespace every call**: `py_dict = PyC_DefaultNameSpace(filepath)` (`:188`, and `:168`
    for text blocks). `PyC_DefaultNameSpace()` builds a brand-new `__main__` module, temporarily
    installs it in `sys.modules`, and sets `__file__`/`__builtins__`
    (`source/blender/python/generic/py_capi_utils.cc:1234-1249`), restoring the previous `__main__`
    afterwards (`PyC_MainModule_Backup`/`_Restore`, `:1272-1292`). So **nothing persists between
    calls** — no variables, no imports, no state.
  - Compilation+eval via `PyRun_FileExFlags`, or `Py_CompileStringObject` + `PyEval_EvalCode` on
    Windows (`bpy_interface_run.cc:96-127`).
  - On failure: `BPy_errors_to_report(reports)` (`:202`) and `PyErr_Clear()` (`:216`) — the
    exception is turned into operator reports, not re-raised to the caller.
  - No stdout/stderr capture: it only **flushes** them (`PyC_StdFilesFlush()`, `:231`).
- Because the failure becomes an `RPT_ERROR` report, the Python caller sees a `RuntimeError`
  (see §2). **[verified 5.2.2]** running `--factory-startup -b --python t_final.py`:

  | Script | Observable result |
  | --- | --- |
  | prints then succeeds | `print()` text appears on the real stdout; returns `{'FINISHED'}` |
  | syntax error | `RuntimeError: Error: Python:   File ".../bad_syntax.py", line 1 … SyntaxError: invalid syntax` + `Location: /private/tmp/bc-research/t_final.py:31` |
  | runtime error | `RuntimeError` whose message contains the full traceback of the script file, plus `Location:` = the *caller's* line |
  | missing file | `RuntimeError: Error: Python: OSError: Python file "…/nope.py" could not be opened: No such file or directory` |

  So the traceback text is recoverable — but as *message text in an exception*, and `Location:`
  points at your addon's call site, not at the failing statement. Output is **not** captured.

### 1c. C-level string runners you cannot use

`BPY_run_string_exec` / `BPY_run_string_eval` / `BPY_run_string_exec_with_locals` exist
(`source/blender/python/BPY_extern_run.hh:99-106`, `bpy_interface_run.cc:341-357, 375-440`) and are
what `--python-expr` uses (`source/creator/creator_args.cc:2820`), but they are **C-only** — no
`bpy` binding. They also use a throwaway namespace (`PyC_DefaultNameSpace("<blender string>")`,
`bpy_interface_run.cc:312`; `PyC_DefaultNameSpace("<BPY_run_string_exec_with_locals>")`, `:388`) and
do not capture output. There is no supported bpy API that runs a string and returns its stdout.

### 1d. The pattern that actually works (and its gotchas)

Stdlib only, exactly as the console does:

1. `code = compile(source, "<model>", "exec")` — do the compile step separately so `SyntaxError`
   carries `lineno`/`offset` for a precise error message.
2. `with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):` then
   `exec(code, globals_ns, locals_ns)` inside `try/except BaseException`.
3. Format with `traceback.format_exception(type(e), e, e.__traceback__)`; `e.__traceback__.tb_lineno`
   gives the failing line.

**[verified 5.2.2]** on both background and live-GUI runs: stdout and stderr were separated
correctly, the `<model>` frame appeared with correct line numbers inside the traceback, and
`traceback.format_exc()` produced a full traceback. Concrete gotchas found by the same probes:

- `exec(code, {})` with a bare dict does **not** become `__main__`: `ns is
  sys.modules["__main__"].__dict__` → `False`. Code that needs `__main__` (pickle, `multiprocessing`,
  `dataclasses` module lookups, `if __name__ == "__main__":`) silently misbehaves. Pass a real
  `ModuleType("__main__").__dict__` if you care, or accept that `__name__` is wrong.
- `sys.stdout` in Blender *is* the process stdout (`<stdout>` `TextIOWrapper`), and
  `redirect_stdout` works by swapping the process-global `sys.stdout`. So any other thread printing
  during the exec gets captured too, and a long-running background thread can write into your buffer.
- `contextlib.redirect_stdin` does not exist in the stdlib; the console defines its own subclass
  (`_console_python.py:166-168`). If the model's code touches `input()`, it will hang the GUI
  event loop — the console's `redirect_stdin(None)` (`_console_python.py:162-163`) is explicitly
  there because *"it can lock Blender"*.
- `SystemExit` must be caught explicitly (the console does, `_console_python.py:178-183`) — otherwise
  the model calling `exit()`/`sys.exit()` interacts with Blender's exit path.
- Neither approach bounds runtime or is interruptible; a `while True:` in the model's code freezes
  the GUI. Nothing in this research found a supported way to time-bound or cancel an `exec` (see
  *What I could not determine*).

---

## 2. How `bpy.ops` failures surface to Python

Three distinct outcomes, all verified on 5.2.2 unless noted:

**(a) Return value, no exception.** Operators return a `set`, e.g. `{'FINISHED'}`, `{'CANCELLED'}`,
`{'RUNNING_MODAL'}`, `{'PASS_THROUGH'}` —
<https://docs.blender.org/api/current/bpy.ops.html>: *"the latter meaning that the operator
execution was aborted without making any changes or saving an undo history entry. If operator was
cancelled but there wasn't any reports from it with `{'ERROR'}` type, it will just return
`{'CANCELLED'}` without raising any exceptions."* Verified: a minimal operator returning
`{'CANCELLED'}` produced `{'CANCELLED'}` and nothing else.

**(b) `RuntimeError`.** Raised for poll failure *and* for any error report. Mechanism, in
`source/blender/python/intern/bpy_operator_function.cc`:

- Poll failure — `:291-300`:
  `PyErr_Format(PyExc_RuntimeError, "Operator bpy.ops.%.200s.poll() %.200s", …, msg ? msg :
  "failed, context is incorrect")`, where `msg` comes from `CTX_wm_operator_poll_msg_get()`.
- After a successful poll, the operator runs with its own `ReportList` (`:305-333`) and then
  `error_val = BPy_reports_to_error(reports, PyExc_RuntimeError, false)` (`:339-341`). Reports are
  *also* echoed to the terminal (`:346-356`).
- Docs confirm it happens regardless of return value:
  <https://docs.blender.org/api/current/bpy.ops.html> — *"if there are error reports, a
  `RuntimeError` will be raised after the operator finishes execution, including all error report
  messages, regardless of the return status (even if it was `{'FINISHED'}`)"*.
- Other error types from the same call path: `AttributeError` if the operator id does not exist
  (`:259-265`), `TypeError` for a bad execution-context string (`:283-289`), `ValueError` for bad
  positional args (`:115-150`), and `RuntimeError: "Context is None, cannot poll any operators"`
  (`:246-249`) if an addon calls in with no context.

**[verified 5.2.2]** exact strings produced:

| Situation | Result |
| --- | --- |
| `bpy.ops.console.scrollback_append(text="x")` with no CONSOLE area | `RuntimeError: Operator bpy.ops.console.scrollback_append.poll() failed, context is incorrect` |
| same, with the operator calling `cls.poll_message_set("custom reason: …")` | `RuntimeError: Operator bpy.ops.bc_research.pollmsg.poll() custom reason: no CONSOLE area here` |
| `self.report({'ERROR'}, "an error report from the operator")` then `{'CANCELLED'}` | `RuntimeError: Error: an error report from the operator` (and the same text printed to stderr) |
| `raise ValueError("boom")` inside `execute()` | `RuntimeError: Error: Python: Traceback (most recent call last): … ValueError: boom … Location: <caller>` |

**Yes, `poll_message_set` lets the caller learn why.** It is a classmethod on the operator class
(`source/blender/python/intern/bpy_rna_operator.cc:88-140`; docs
<https://docs.blender.org/api/current/bpy.types.Operator.html#bpy.types.Operator.poll_message_set>),
accepting a string or a callable + args, and it feeds the same text that ends up in the
`RuntimeError`. The default text when an operator does not set one is the generic
`"failed, context is incorrect"`.

**They are catchable.** Verified `except RuntimeError:` catches all of the above. Practical pattern:
`if not cls.poll(): …` for cheap pre-checks (`bpy_operator_function.cc:213-230`), but the *reason* is
only available via the exception message — so wrap the call and parse/inspect the message, and
distinguish "poll" (`"poll()"` in the text) from "execute failed".

**Blunt gaps.**

- A poll failure and a failing `execute()` are indistinguishable by exception *type* — both
  `RuntimeError`. The only signal is the message text, which is not a stable API.
- `{'CANCELLED'}` **with no report** is a completely silent no-op. The model's code gets a dict back
  and nothing else. If the addon only surfaces stdout/stderr and exceptions, "the operation silently
  did nothing" is invisible to the model. The tool contract must treat anything other than
  `{'FINISHED'}` as a failure and say so.
- The report text is printed to stdout/stderr *as well as* raised, so a naive
  "concat stdout+stderr+exception" capture duplicates it.

---

## 3. What is undoable: RNA writes vs `bl_options={'REGISTER','UNDO'}` vs no-undo, and mixtures inside one `exec()`

### 3a. Undo is explicit and snapshot-based

- `ED_undo_push()` (`source/blender/editors/undo/ed_undo.cc:103-152`) creates a step that encodes
  the **current whole state** (`BKE_undosys_step_push`, `:144`). For the global/memfile undo type
  that encoding is a full copy of `Main` (a blend-file written to memory) —
  `source/blender/editors/undo/memfile_undo.cc:74-96` → `BKE_memfile_undo_encode()`.
- Undo means *load the previous step's snapshot*: `BKE_undosys_step_undo()` =
  `…load_data_ex(…, step_active->prev, …)` (`source/blender/blenkernel/intern/undo_system.cc:906-911`).
  Redo loads `step_active->next` (`:933-938`).

Consequence: undo does not "reverse an action", it **restores a whole snapshot**.

### 3b. Operators called from Python never push

The gate is `wm->op_undo_depth`:

- `wm_operator_finished()` only pushes a step when `wm->op_undo_depth == 0`
  (`source/blender/windowmanager/intern/wm_event_system.cc:1296-1318`, incl. `ED_undo_push_op` at
  `:1305` and `ED_undo_grouped_push_op` at `:1311`), and operator registration/repeat needs the same
  (`wm_operator_register_check`, `:1264-1273`).
- `WM_operator_call_py()` deliberately sets that guard for Python callers —
  `wm_event_system.cc:1999-2018`, with the comment: *"Not especially nice using undo depth here. It's
  used so Python never triggers undo or stores an operator's last used state."*
- The Python call path passes `is_undo` through: `bpy_operator_function.cc:327`
  (`WM_operator_call_py(C, ot, context, &ptr, reports, is_undo)`), and `is_undo` is the **second
  positional argument** of every `bpy.ops` call (`bpy_operator_function.cc:115-150`;
  documented as `bpy.ops.test.operator(execution_context, undo)` —
  <https://docs.blender.org/api/current/bpy.ops.html>).

**[verified 5.2.2, background]** after `bpy.ops.ed.undo_push(message="A")` (the only step) and then
`bpy.ops.mesh.primitive_cube_add()`:

```
objects: ['Camera', 'Cube', 'Cube.001', 'Light']
ed.undo.poll() after op: False          # no new step appeared
undo raised: RuntimeError | Operator bpy.ops.ed.undo.poll() failed, context is incorrect
objects after attempted undo: ['Camera', 'Cube', 'Cube.001', 'Light']   # not reverted
```

and with `bpy.ops.object.delete()`: object gone, `undo` poll-failed, `['Camera', 'Light']` stayed.
**[verified 5.2.2, live GUI timer]** undo went straight past both Python-called operators to the
earlier pushed snapshot, removing the operator-created `Cube.001` — consistent with only the two
explicitly pushed steps existing.

So: `bl_options = {'REGISTER', 'UNDO'}` on your own operator is meaningless for undo when the
operator is invoked from Python — including from your addon, a `bpy.app.timers` callback, or a
`bpy.ops` call in model-authored code. (Contrast: the same operator run from a menu/keymap, where
`op_undo_depth == 0`, does push a step. This is why the Python Console gets undo steps but a script
does not.)

To opt in from Python you must pass the boolean: `bpy.ops.object.some_op('EXEC_DEFAULT', True)`
(**source-only**; read from `bpy_operator_function.cc:115-150` + `wm_event_system.cc:2005-2011`, not
executed).

### 3c. Direct RNA writes never push, and un-pushed work is destroyed

There is no script-facing hook from RNA writes to undo. `rna_access.cc` contains only
`RNA_property_undo_check()` (`:2327-2337`), a *query* used by UI buttons
(`interface.cc:1303-1310`, `interface_ops.cc:346`) to decide whether to push after a widget edit.
All ~60 `ED_undo_push(...)` call sites are in C UI code (grep across the tree), e.g.
`interface_handlers.cc:1258-1265`, `object_modes.cc:504-533`.

Therefore a Python write is captured only if *something later* pushes a snapshot — and then it is
captured as part of that snapshot, not as its own action. **[verified 5.2.2]** (background and GUI,
identical):

```
push A  (loc = 0,0,0)
loc = (1,1,1)                       # not pushed
push B                              # snapshot contains (1,1,1)
loc = (5,5,5)                       # not pushed
undo  -> loc (0,0,0)                # restores step A
redo  -> loc (1,1,1)                # (5,5,5) is GONE, not on the redo stack
```

This is the single most dangerous property for this design: **the newest un-pushed change is
destroyed by the next undo and is not recoverable by redo.**

### 3d. A mixture inside one `exec()` — one step, several, or inconsistent?

Depends entirely on what you do, and the middle case is the trap:

| Turn contains | Result |
| --- | --- |
| only RNA writes, and the addon pushes once **after** the turn | **one** step for the whole turn, named by your `message`. The user's Ctrl+Z lands on the previous step (the pre-turn state) and reverts exactly the turn. This is the good case. **[verified 5.2.2]** |
| only RNA writes, and the addon pushes once **before** the turn (nothing after) | undone step is the *pre-turn* one, so Ctrl+Z lands on the step **before** that and reverts **too far** — the turn *and* everything since the previous step. **[verified 5.2.2]**: `(1,1,1) → push → (2,2,2) turn → undo → (0,0,0)` |
| only RNA writes, no push at all | **zero** steps. Undo jumps to whatever the user last did and silently discards the turn's work — **[verified 5.2.2]**: a turn leaving `loc = (8,8,8)` disappeared when an unrelated undo ran, landing on `(4,4,4)` |
| operators only (via `bpy.ops`) | **zero** steps from those operators |
| operators called with `undo=True` | the operator pushes its own step(s) → **several** steps in one turn, interleaved by timing, so one Ctrl+Z undoes only part of the turn. |
| mixture of RNA writes + operators, one push after the turn | one step for the RNA writes; the operators contribute nothing. Content-wise still one step, but the *order* inside it is invisible. |
| your push happens while a user modal operator is mid-flight | the user's operator will push its own step when it finishes (`wm_event_system.cc:2998-3020`), producing two steps and a confusing history. |

Blender's own documentation is blunt about the no-push case:
<https://docs.blender.org/api/current/bpy.types.Operator.html> ("Modifying Blender Data & Undo") —
*"Otherwise, no undo step will be created, which will at best corrupt the undo stack and confuse the
user (since modifications done by the operator may either not be undoable, or be undone together with
other edits done before). In many cases, this can even lead to data corruption and crashes."* and
*"Note that when an operator returns `{'CANCELLED'}`, no undo step will be created. This means that
if an error occurs after modifying some data already, it is better to return `{'FINISHED'}`."*

### 3e. Other conditions that silently disable the snapshot

- **Global Undo off** → no memfile steps at all: `memfile_undosys_poll()` returns false when
  `(U.uiflag & USER_GLOBALUNDO) == 0` (`memfile_undo.cc:58-70`), i.e.
  `preferences.edit.use_global_undo` (`rna_userdef.cc:5807-5808`) is unchecked. The addon should
  check this and warn.
- **Edit mode / sculpt etc.**: `ED_undo_is_memfile_compatible()` returns false when the active
  object is in `OB_MODE_EDIT` (`ed_undo.cc:585-600`), so the global memfile poll declines and
  mode-specific undo systems take over. A "global" push in that state does not behave like a scene
  snapshot.
- **Undo limits**: `U.undosteps` / `U.undomemory` prune the stack as you push
  (`ed_undo.cc:103-152`; `BKE_undosys_stack_limit_steps_and_memory`). A turn-per-snapshot policy
  will evict the user's earlier history if it is chatty.

---

## 4. `bpy.ops.ed.undo_push` — poll, no-stack behaviour, timers, undo/redo, surviving errors

- Definition: `ED_OT_undo_push` — `source/blender/editors/undo/ed_undo.cc:818-838`. Name
  `"Undo Push"`, description **"Add an undo state (internal use only)"** (`:821-823`), idname
  `ED_OT_undo_push` (`:823`). Property `message` (string, `BKE_UNDO_STR_MAX`), default
  `"Add an undo step *function may be moved*"` (`:831-838`). Docs list it as
  <https://docs.blender.org/api/current/bpy.ops.ed.html#bpy.ops.ed.undo_push>.
- **Poll requirement**: `ot->poll = ED_operator_screenactive` (`:828`) — an active *screen*, not an
  area, and explicitly not an initialised undo stack: the comment at `:826-827` says *"Unlike others
  undo operators this initializes undo stack."*
- **No undo stack**: `ed_undo_push_exec` (`:727-735`) creates one when `G.background` and
  `wm->runtime->undo_stack == nullptr` (`:728-734`), then calls `ED_undo_push(C, str)` (`:733`).
  **[verified 5.2.2]**: in `-b` with `--factory-startup`, `bpy.ops.ed.undo_push.poll()` → `True`,
  the call returns `{'FINISHED'}`, and after it `bpy.ops.ed.undo.poll()` becomes meaningful.
- **Error text when there is no stack**: `ed_undo_is_init_poll` sets
  `"Undo disabled at startup in background-mode (call `ed.undo_push()` to explicitly initialize the
  undo-system)"` (`ed_undo.cc:785-795`). **[verified 5.2.2]** that exact string is what
  `bpy.ops.ed.undo()` raises in an un-initialised background session.
- **From a timer callback with a minimal context**: **[verified 5.2.2, live GUI]** inside
  `bpy.app.timers.register(fn)`:
  `bpy.ops.ed.undo_push.poll()` → `True`, `bpy.ops.ed.undo_push(message="A")` →
  `{'FINISHED'}`. Also in the same timer context: `bpy.context.area` is `None` while
  `bpy.context.screen` is a valid screen. So **screen-polling operators work from a timer;
  area-polling operators will poll-fail** — which matches `ED_operator_screenactive`. Note timers do
  not run under `-b` at all, so this path is GUI-only.
- **`bpy.ops.ed.undo()` / `bpy.ops.ed.redo()`**: `ED_OT_undo` (`:806-816`) / `ED_OT_redo` (`:849-859`),
  both with `poll = ed_undo_poll` / `ed_redo_poll` (`:797-805`, `:841-847`), which require
  `ed_undo_is_init_and_screenactive_poll` **and** a step with a `prev`/`next`
  (`undo_stack->step_active->prev != nullptr` etc.). **[verified 5.2.2]** `bpy.ops.ed.undo()`
  returned `{'FINISHED'}` from inside a timer in the live GUI. `bpy.ops.ed.undo_history(item=N)`
  also exists (docs) but was not exercised.
- **Does the stack survive a script error?** Yes. **[verified 5.2.2]** a raised error inside `exec`
  left the stack intact: pushes and a subsequent `bpy.ops.ed.undo()` all worked, and the undo
  restored the snapshot (including the partial mutation the failed script had made *before* raising).
  The stack is a `wmWindowManager` structure; a Python exception unwinds only Python frames.
  The reverse is also true and more important: a failed script's **partial** mutations stay in the
  live data and are only visible to undo if some step was pushed.
- **Detecting undo from the addon**: `bpy.app.handlers.undo_pre / undo_post / redo_pre / redo_post`
  all exist and are fired via `BKE_callback_exec_id(..., BKE_CB_EVT_UNDO_PRE/POST, ...)`
  (`ed_undo.cc:168-217`). **[verified 5.2.2]** all four are present (and empty by default). This is
  the supported way to invalidate addon-side caches when the user undoes.
- **`bpy.ops.ed.undo_push` is officially an advanced/internal facility**:
  <https://docs.blender.org/api/current/bpy.types.Operator.html> — *"Such manual undo push is
  possible using the `bpy.ops.ed.undo_push` function. Be careful though, this is considered an
  advanced feature and requires some understanding of the actual undo system in Blender code."*

---

## 5. Is `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)` a safe, cheap pre-turn snapshot?

### 5a. Semantics (source + verified)

- `wm_save_as_mainfile_exec` (`source/blender/windowmanager/intern/wm_files.cc:3991-4060`):
  `use_save_as_copy = is_save_as && RNA_boolean_get(op->ptr, "copy")` (`:3996`).
- **Filepath is not changed for a copy**: `if (use_save_as_copy == false) { STRNCPY(bmain->filepath,
  filepath); }` (`wm_files.cc:2223-2225`). **[verified 5.2.2]** `bpy.data.filepath` stayed `''`
  before and after a copy save of an unsaved file, and stayed `…/base/scene2.blend` when a real file
  was open.
- **Dirty state is not cleared**: `bpy.data.is_dirty` is `!wm->file_saved` (`rna_main.cc:66-70`),
  and `wm->file_saved` is only cleared by `WM_file_tag_modified()` (`wm_files.cc:185-192`), whose
  callers are undo pushes and a handful of explicit sites. `save_as_mainfile` sets it only for real
  saves. **[verified 5.2.2]** `is_dirty` was unchanged by a copy save.
- **It flushes edits first**: `ED_editors_flush_edits(bmain)` (`wm_files.cc:2206`) runs before
  writing. This can mutate session state (flushing edit-mode/paint data into the data-blocks) — a
  side effect on the user's live session, not just a read.
- **It remaps relative paths by default**: `relative_remap` defaults to `true`
  (`wm_files.cc:4199-4203`) → `BLO_WRITE_PATH_REMAP_RELATIVE` (`:4016-4021`). **[verified 5.2.2]**
  in a single-image, no-library test the in-memory `image.filepath` was unchanged with both
  `relative_remap=True` and `False`; treat libraries/hair/sequence/cache paths as **not determined**.
- **It writes to disk and rotates a backup**: `blend_write_params.use_save_versions = true`
  (`wm_files.cc:2213`) — **[verified 5.2.2]** overwriting the same target produced a `.blend1`
  sibling: `heavy.blend` (31.9 MB) + `heavy.blend1` (31.9 MB). Repeating a snapshot at the same path
  therefore costs ~2× the file size on disk.
- **Python-issued saves do not pollute the recent-files list**: `do_history_file_update =
  (G.background == false) && (CTX_wm_manager(C)->op_undo_depth == 0)` (`wm_files.cc:2220-2221`) —
  and a `bpy.ops.*` call from Python has `op_undo_depth >= 1` (`wm_event_system.cc:2005-2009`).
  **source-only inference**; not measured.
- Operator flags: `WM_OT_save_as_mainfile` sets no `OPTYPE_UNDO`/`REGISTER`
  (`wm_files.cc:4166-4204`), so it never contributes an undo step. Description of `copy`: *"Save a
  copy of the actual working state but does not make saved file active"* (`:4152-4157`).
- Because it writes a *file*, **undo cannot undo it** (§6).

### 5b. Measured cost on a large scene [verified 5.2.2, this machine]

Synthetic scene: a `1000×1000` grid (1,002,001 verts / 1,000,000 polys) plus 200 objects sharing a
5k-vert mesh → 204 objects, 3 meshes, 1,007,009 verts.

| Operation | Time (min–max of 3) | Artifact |
| --- | --- | --- |
| `bpy.ops.ed.undo_push(message=…)` | 0.001–0.005 s | RAM (memfile undo step) |
| `bpy.ops.wm.save_as_mainfile(copy=True)` | **0.058–0.070 s** | 31.9 MB file |
| `…, compress=True` | 0.054–0.059 s | 31.9 MB file (data already compact) |
| repeat copy save | 0.059–0.064 s | + a `.blend1` |
| empty factory scene copy save | 0.003–0.041 s | 96 KB |

Interpretation and the honest limits of that number:

- Cost tracks **bytes of datablocks**, not object count. A 32 MB scene copies in ~60 ms; the cost
  will scale roughly with file size. A production file with gigabytes of packed 4K textures, caches,
  particles or simulation data will take seconds, not milliseconds, **and** the snapshot consumes
  that much disk (2× with the `.blend1` rotation). I did not measure a real heavy production file
  (see *What I could not determine*).
- `ed.undo_push` is ~10–50× cheaper here **and** it is what makes the turn undoable in Blender's own
  UI, which is the behaviour a user expects from Ctrl+Z. Its cost is RAM: memfile undo keeps an
  encoded copy of the data and dedupes against the previous step (`memfile_undo.cc:74-96`).
  `bpy.app.memory_usage_undo()` exists to measure that
  (<https://docs.blender.org/api/current/bpy.app.html>); it returned `0` in background before any
  stack existed, so measure it in the GUI.

### 5c. Verdict

`copy=True` is **not** a cheap-or-free primitive and it is not a safety net for undo — undo and the
file on disk are separate mechanisms, and the file is unreachable by Ctrl+Z. It is defensible as a
*durable* recovery artefact only if all of these hold: the file is not enormous; it is written to a
private directory (never beside the user's file); a fresh filename is used per snapshot (or the
`.blend1` doubling is accepted); the user is told the file exists and where; and `relative_remap`
is decided deliberately. The lighter alternative for the "one undoable step per turn" behaviour is
`bpy.ops.ed.undo_push(message="<model/turn label>")`, with the §3c caveat that anything not pushed by
the time the user hits Ctrl+Z is lost.

---

## Undo: covers vs does not cover

Read this as: *what a correctly-pushed undo step can restore, and what it can never touch.*

### Covers (only after an explicit push / a UI-driven operator push)

| Thing | Notes |
| --- | --- |
| Local `ID` data in `bpy.data`: objects, meshes, materials, node trees, scenes, collections, actions, worlds, images (datablock state), texts, lattices, armatures… | The memfile step is a full encode of `Main` (`memfile_undo.cc:74-96`). Effects of `bpy.data.*.new()/remove()` and property writes are included **if a step is pushed after them** |
| Deletions, if a step predates them | Verified: an API-created object + mesh disappeared on undo to an earlier step; the reverse (restoring a deleted object) works the same way |
| Operator edits, when the operator was run from the UI (menu/keymap) or called with the `undo=True` positional arg | `wm_operator_finished` gate at `wm_event_system.cc:1296-1318` |
| Selection / active object / mode changes | only as part of a snapshot; never individually |
| Direct RNA writes from Python | **only** as part of a later snapshot; never their own step |
| Side effects of `ED_editors_flush_edits` at push time | edit-mode/paint data is flushed into the ID before encoding — which also means *your push* can change the session |

### Does not cover — and some of this can never be covered

| Thing | Why |
| --- | --- |
| **Filesystem** — every file created, overwritten or deleted by the code (`open(...,'w')`, `os.remove`, `shutil.rmtree`, `img.save()`, exporters, `bpy.ops.wm.save_mainfile*`) | Not part of `Main`; no undo system in Blender touches the filesystem. **Undo can never reach the filesystem.** A snapshot *file* is itself a file on disk that undo cannot delete |
| **Subprocesses** — `subprocess`, `os.system`, launched render jobs | Effects are outside Blender and irreversible; also the fastest way to hang or kill the session |
| **Network** — HTTP requests, uploads, anything already sent | Irreversible; see §7 |
| Render results in memory (`bpy.data.images['Render Result']`) and GPU/viewport/compositor state | Not an undoable ID; render output written to disk is a file |
| Screens, window manager, workspaces | `ID_CHECK_UNDO` excludes `ID_SCR, ID_WM, ID_WS, ID_BR` (`source/blender/makesdna/DNA_ID.h:686`); `IDTYPE_FLAGS_NO_MEMFILE_UNDO` on screen (`screen.cc:282`), workspace (`workspace.cc:235`), wm (`wm.cc:250`) |
| Brushes (and with them paint-mode settings) | `IDTYPE_FLAGS_NO_MEMFILE_UNDO` (`brush.cc:600`); also excluded by `ID_CHECK_UNDO` |
| Addon preferences, `bpy.context.preferences.*`, `userpref.blend`, `bookmarks.txt`, `recent-files.txt` | Different file, different mechanism, no undo (§8) |
| **Python state**: module globals, `sys.modules`, registered classes/panels/handlers/timers, open sockets/files, threads | Undo restores data, not code. A second turn's code sees whatever the first turn left in memory |
| The undo stack itself | `bpy.ops.wm.open_mainfile` / `read_factory_settings` / `read_homefile` **clear** it (`wm_files.cc:824-833`; verified on 5.2.2: after `read_factory_settings`, `ed.undo_push.poll()` is fine but `bpy.ops.ed.undo()` raises the "Undo disabled at startup in background-mode" message) |
| Linked-library data and library state | Linked IDs are re-resolved; undo steps only write placeholders for them (`writefile.cc:1386-1400`) |
| The "last operator" / Adjust-Last-Operation panel for Python-called operators | `wm_operator_register_check` requires `op_undo_depth == 0` (`wm_event_system.cc:1264-1273`) |
| Changes made **after** the newest push | They are wiped by the next undo and are *not* on the redo stack. **Verified twice** (background + GUI): `(5,5,5) → undo → (0,0,0) → redo → (1,1,1)` |
| Anything when Global Undo is off | `memfile_undosys_poll` declines (`memfile_undo.cc:58-70`) |
| Global steps while the active object is in edit mode | `ED_undo_is_memfile_compatible` returns false (`ed_undo.cc:585-600`); mode-specific undo applies instead |
| Data older than the step-count / memory limits | `U.undosteps` / `U.undomemory` pruning (`ed_undo.cc:103-152`) |
| Blender's own process liveness, crashes, `bpy.ops.wm.quit_blender()` | Nothing to undo after the process is gone; unsaved work is gone |

One-line summary: **undo covers the local `bpy.data` database, by snapshot, only when a step was
explicitly pushed; it can never reach anything outside the process — and not even everything inside
it.**

---

## Snapshot recipe

Cheapest-first; pick per turn based on what the turn is allowed to touch.

1. **Gate the turn.** Record `preferences.edit.use_global_undo`, whether the active object is in
   edit mode, `bpy.data.is_dirty`, `bpy.data.filepath`, and `bpy.data.is_saved` before running any
   model code. If global undo is off, or a modal operator is running, say so instead of promising
   undo.
2. **Primary boundary: one explicit undo step per turn, pushed at the END of the turn.**
   `bpy.ops.ed.undo_push(message="<short turn label>")` immediately **after** the turn's last
   mutation, and confirm `{'FINISHED'}`. Cost is ~0.001–0.005 s on a 1M-vert scene; the poll needs
   only an active screen (works from a timer — verified), the exec initialises the stack itself
   (`ed_undo.cc:727-735`), and the label shows up in Undo History.
   **Why the end and not the start — [verified 5.2.2]:** undo loads `step_active->prev`
   (`undo_system.cc:906-911`), so the step the user's Ctrl+Z *lands on* is the one that defines what
   is reverted.
   - push **after** the turn → Ctrl+Z lands on the previous step = pre-turn state → **exactly the
     turn is reverted**. Measured: pre-turn `(3,3,3)`, turn sets `(4,4,4)`, push, undo → `(3,3,3)`,
     redo → `(4,4,4)`.
   - push **before** the turn (and nothing after) → Ctrl+Z lands on the step *before* that and
     **reverts too far**, discarding the turn and everything since the previous step
     (`(1,1,1) → push → turn to (2,2,2) → undo → (0,0,0)`).
   - no push at all → the turn is discarded by an unrelated undo (`turn to (8,8,8)` → undo →
     `(4,4,4)`).
3. **Make sure a pre-turn snapshot exists.** The step the user's Ctrl+Z lands on must be the
   pre-turn state. If the user's own last action pushed a step (any UI operator does), that step is
   it and step 2 alone is enough. If nothing has been pushed for a while — or the addon is the only
   actor, as in a scripted/selftest session — push once *before* the turn as well, so the boundary is
   explicit. In an interactive GUI session this second push is usually unnecessary; in a headless or
   timer-driven session it is not.
4. **Never rely on the model's own operators for undo.** Anything the model does via `bpy.ops.*`
   pushes nothing. The turn's step is the *only* step the turn gets — so the step must be pushed by
   the addon, not by the code under test.
5. **Optional durable fallback: a private copy.** Only when the turn may be destructive and the user
   has consented to the disk cost:
   `bpy.ops.wm.save_as_mainfile(filepath=<private-dir>/<unique-name>.blend, copy=True,
   relative_remap=False)`. Expect: no change to `bpy.data.filepath`, no change to `is_dirty`, an
   `ED_editors_flush_edits` side effect, a real write (~60 ms / 32 MB on the measured scene, more on
   a texture-heavy file), and a `.blend1` sibling if the target already exists — so use a unique
   filename per snapshot. Tell the user where the file is; it is a file, and undo cannot remove it.
6. **Never claim undo covers what it does not.** Before any turn that touches the filesystem,
   subprocesses, network, or preferences, say plainly that undo does not apply and that the only
   mitigation is not doing it (or doing it in a scratch directory the addon created and can clean up).
7. **Invalidate on external undo.** Register `bpy.app.handlers.undo_post` / `redo_post` to drop
   cached scene references and reconcile the conversation with the new state; do not hold `Object`/
   `Mesh` Python references across a push/undo boundary (undo re-creates data-blocks).
8. **Detect the file being swapped under you.** `bpy.ops.wm.open_mainfile` / `read_factory_settings`
   clear the undo stack and reset `bpy.data`; treat them as "conversation state invalid" and re-snapshot.

---

## What I could not determine

Ordered by how much it would change the design if it came back the other way.

1. **Real production-file snapshot cost.** My heaviest probe was a synthetic 32 MB scene
   (1M verts, 204 objects). No measurement of a multi-GB file with packed 4K textures, caches,
   particles, or simulations — which is where `copy=True` turns from "60 ms" into "seconds plus
   gigabytes of disk". Needs a real file from the user.
2. **Timer/undo races against in-flight user interaction.** I verified push/undo from a timer on an
   idle session. I did not test pushing while the user is mid-transform, mid-modal-operator, or
   mid-render, nor whether a push inside a timer can interleave badly with an operator finishing in
   the same event-loop iteration. `wm->op_undo_depth` is the guard, and a timer callback does not run
   *inside* an operator, so it **should** be safe — but that is reasoning, not measurement.
3. **`undo=True` on `bpy.ops` end-to-end.** Read from source (`bpy_operator_function.cc:115-150`,
   `wm_event_system.cc:2005-2011`) and documented, but not executed. If the addon wants the model's
   operators to be individually undoable this must be validated first, including how many steps a
   single turn then produces.
4. **Undo in edit/sculpt/paint modes and with mode-specific undo systems.** Only the
   `ED_undo_is_memfile_compatible` early-out was read (`ed_undo.cc:585-600`); no probes in edit mode.
5. **What exactly a memfile step restores for `Image`.** I verified only that an Image *datablock*
   existed and survived; not whether loaded pixel buffers, packed data, or in-memory `pixels`
   modifications are faithfully restored.
6. **`relative_remap` on a real linked/asset-heavy file.** My test had one image and no libraries, and
   showed no change to in-memory paths in either setting. Libraries, hair, VSE, and caches are
   untested.
7. **Undo memory in a GUI session.** `bpy.app.memory_usage_undo()` returned `0` in background mode
   before the stack was initialised; I did not measure the RAM cost of a turn-per-snapshot policy in
   the GUI, which is what decides whether `undo_push` or the file copy is actually cheaper for the user.
8. **Whether an `exec` can be time-bounded or cancelled at all.** Nothing found in this research: no
   supported interruption API for a Python `exec` running on the main thread, so a `while True:` in
   model-authored code freezes the GUI with no recovery. `redirect_stdin(None)` only prevents the
   stdin hang.
9. **`bpy.ops.ed.undo_history(item=N)`** behaviour (exists per docs; not exercised).
10. **5.2 vs 5.3 drift.** All source citations are the 5.3.0-alpha clone; all runtime evidence is
    5.2.2. Claims marked **source-only** are the ones most exposed to drift.
11. **macOS Keychain storage for the API key.** Out of scope here; established elsewhere that `keyring`
    is not bundled, and §8 confirms preferences are plaintext. The secure alternative (Keychain via
    `security` CLI or `ctypes`) was not researched.
12. **Whether `save_as_mainfile(copy=True)` writes a thumbnail side-effect** (there is thumbnail
    creation code in `wm_file_write` after the write, `wm_files.cc:2231-2243`) and whether a
    Python-issued copy save still touches `recent-files.txt`. The latter I inferred as *no* from
    `wm_files.cc:2220-2221` + `wm_event_system.cc:2005-2009`, but did not observe in a GUI session.

---

## Appendix: probe inventory (for reproduction)

All under `/tmp/bc-research/`, run against `/Applications/Blender.app/Contents/MacOS/Blender` 5.2.2
LTS (`build hash d13f752e3b9c`, built 2026-09-15).

| Probe | Mode | What it established |
| --- | --- | --- |
| `t_exec.py` | `--factory-startup -b` | exec capture (stdout/stderr/traceback), `sys.stdout` identity, poll-failure message text, `poll_message_set`, `CANCELLED` returns, `RuntimeError` shapes |
| `t_undo1.py` | `-b` | undo stack absent in background; `ed.undo_push` initialises it; **operators called from Python create no step** |
| `t_undo2.py` | `-b` | RNA writes vs pushes; un-pushed change destroyed and not redoable; stack survives a script error; API-created IDs removed by undo to an earlier snapshot |
| `t_save.py`, `t_save2.py` | `-b` | copy-save side effects (filepath/dirty unchanged, no remap change), cost and sizes, `.blend1` creation |
| `t_online.py` | `-b`, with and without `--offline-mode` | `online_access` read-only; **HTTPS + raw socket succeed with it False** |
| `t_final.py` | `-b` | `python_file_run` success/syntax/runtime/missing-file; `bpy.ops.object.delete()` leaves nothing undoable; `read_factory_settings` clears the stack; undo/redo handlers exist |
| `t_prefs.py` + `run_prefs.sh` | `-b`, isolated `BLENDER_USER_CONFIG` | preference file location; `strings` reveals a plaintext `api_key`; observed file mode `-rw-r--r--` |
| `t_gui.py` | live GUI, `bpy.app.timers` | **the addon's real path**: `undo_push` works from a timer, `context.area is None` / `screen` set, undo from a timer, un-pushed write lost, operator-created object removed by undo |
| `t_gui2.py` | live GUI, timer | bare RNA writes and `bpy.data.objects.new()` leave `bpy.data.is_dirty == False` |
| `t_recipe.py` | `-b` | the undo step boundary must sit at the **end** of the turn: push-before-only reverts too far; push-after reverts exactly the turn; no push loses the turn to an unrelated undo |
