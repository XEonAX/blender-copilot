"""`run_blender_python`: the sandbox the model's code runs in, minus Blender.

This module imports no `bpy`, and that is the point. Everything `run_blender_python`
*is* - the fresh namespace, the stdout/stderr capture, the traceback extraction,
the truncation caps and the envelope - is exercised on plain CPython
(`tests/test_conversation.py`) with fake bindings. Only the live names themselves
(`bpy`, `C`, `D`, `mathutils`) come from `toolbox.py`, so the only part that needs
a running Blender is the part that genuinely *is* Blender.

The contract is ticket 06's, transcribed:

  * **Fresh namespace per call.** A new `__main__`, installed for the duration and
    restored in `finally`. Persistent state is rejected there for a reason that is
    worth repeating here: undo restores `bpy.data` and never Python state (ticket
    02), so a namespace surviving a Ctrl+Z holds freed datablocks and the next
    call dies with `ReferenceError: StructRNA ... has been removed`.
  * **The full traceback always comes back**, plus the stdout accumulated before
    the raise, because that is the only thing that lets the model fix its own bug.
  * **Caps**: stdout 4,000 / stderr 2,000 / traceback 2,000 chars, middle-elided
    with a literal `… [N chars elided] …`, plus `truncated` and a `note`.
  * **No sandbox, no timeout, no cancel, and no undo.** None of those are claims
    this module makes; they are properties of it, documented in the tool
    description the model reads rather than enforced here.

The two read-only tools live here as well, for the same reason and with the same
shape of seam. They never touch `bpy` either: they read through a **world**, a
duck-typed object `toolbox.LiveWorld` implements against the live build and the
CPython suite implements with `SimpleNamespace` fakes. The world is deliberately
small:

    bindings()            -> dict     the prelude for run_blender_python
    facts()               -> dict     the scalar scene facts
    objects()             -> list     every object, depsgraph already updated
    active_name()         -> str | None
    selected_names()      -> list[str]
    collections()         -> list     nested {"name", "count", "children"}
    type_names()          -> list[str]
    operator_names()      -> list[str]
    rna_of_type(name)     -> rna struct | None
    rna_of_operator(id)   -> rna struct | None
    operator_poll(id)     -> bool | None

That split is the point of the module: *reaching into bpy* is the only part that
needs Blender, and it is the part `tools/read_only_tools_probe.py` checks against
the installed 5.2.2. Everything the model actually receives - validation, filters,
the caps, the truncation flags, the `not_found` matches, the shared 8,000-char
result cap - is decided here, on plain CPython, where it can be disagreed with.

`execute_tool` returns the shape the loop consumes, not a bare envelope:

    {"ok": bool, "envelope": {...}, "content": str, "detail": str, "summary": str}

`content` is the wire `tool` message (compact JSON, shared-capped); `detail` is the
same envelope pretty-printed for the panel's expander. The loop treats them as
opaque strings, which is exactly why `conversation.py` can stay free of this
module.
"""

from __future__ import annotations

import contextlib
import difflib
import importlib.util
import io
import json
import sys
import traceback
import types
from pathlib import Path

# The name Blender's own `code.interact` machinery uses for model code, so a
# traceback frame can be identified by filename.
FILENAME = "<model>"


def _sibling(name: str):
    """Import a bpy-free sibling module, from a package or from this file's folder.

    The CPython suite and `tools/loop_wire_probe.py` both load *this* module by
    path, with no package around it, so a plain `from . import guides` is not
    always available. Try the package first (the addon's own case, and the GUI
    probes'), then the file next to this one.

    The package branch is a relative import of `name`, not of `guides`: the file
    is imported twice, by two different module names, because `budget.LIMITS` has
    to be the *same* object in `execution` and in `conversation` (the call window
    and the turn's clock are two views of one ledger) - and when this module is
    loaded by path the two halves each get their own copy, which is why the
    identity is asserted in `tools/panel_draw_smoke.py` rather than assumed.

    The `bc_` branch is what makes that true under the CPython suite, which has no
    package: a loader that has already read `budget.py` by path registers it as
    `bc_budget`, and both halves then resolve to that one object instead of
    loading a third. The two copies are otherwise indistinguishable, which is
    exactly how an `isinstance` against the wrong copy would go unnoticed.
    """
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception:  # noqa: BLE001 - any failure here means "load it by path"
        cached = sys.modules.get(f"bc_{name}")
        if cached is not None:
            return cached
        path = Path(__file__).with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(f"bc_{name}", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise ImportError(f"cannot load {name} from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"bc_{name}"] = module
        spec.loader.exec_module(module)
        return module


guides = _sibling("guides")
budget = _sibling("budget")

RUN_PYTHON = "run_blender_python"
SCENE_INFO = "get_scene_info"
RNA_INFO = "get_rna_info"

# Ticket 06's caps. stdout keeps head *and* tail (setup prints at the head,
# errors at the tail); stderr and the traceback keep the tail, which is where the
# cause is.
STDOUT_CAP = 4000
STDERR_CAP = 2000
TRACEBACK_CAP = 2000
RESULT_CAP = 8000      # the shared cap, applied to the serialised envelope
RAW_ARGS_CAP = 500     # how much of a malformed arguments string is fed back
PURPOSE_MAX = 80

# --- get_scene_info, ticket 06 §2 -------------------------------------------------
SCENE_SCOPES = ("summary", "selection", "active", "objects")
SCENE_INCLUDES = (
    "transform", "dimensions", "parent", "collections", "materials",
    "modifiers", "constraints", "custom_properties", "mesh_stats",
)
SCENE_FILTER_KEYS = ("types", "name_contains", "collection", "selected_only")
# Include defaults, per scope. `summary` takes none because it aggregates instead
# of listing, which is the whole reason it can answer for a scene of any size.
SCENE_INCLUDE_DEFAULTS = {
    "summary": (),
    "selection": ("transform",),
    "objects": ("transform",),
    "active": ("transform", "dimensions", "materials", "modifiers", "mesh_stats"),
}
SCENE_LIMIT = 25
SCENE_LIMIT_MAX = 100
SELECTION_NAME_CAP = 8   # ticket 06: `selection:{count,names<=8}` in the summary
NAME_LIST_CAP = 20       # `collections` on one object
COLLECTION_TREE_CAP = 30 # nodes emitted in the summary's `collection_tree`
CUSTOM_PROP_CAP = 10     # keys per object
CUSTOM_PROP_VALUE_CAP = 200
DESCRIPTION_CAP = 240    # RNA descriptions, middle-free: head only, with a marker

# --- get_rna_info, ticket 06 §3 ---------------------------------------------------
RNA_KINDS = ("type", "operator", "search", "guide")
RNA_PROPERTY_LIMIT = 40
RNA_SEARCH_LIMIT = 50
RNA_LIMIT_MAX = 200
ENUM_ITEM_CAP = 30       # with `enum_count` always present
MATCH_LIMIT = 8          # ticket 06: `not_found` returns 5-10 close matches

# --- the schemas -------------------------------------------------------------

# The tool descriptions are prompt surface (ticket 10 §Surface 2): only what the
# JSON schema cannot say - behaviour, traps and the interface facts a parameter
# name cannot carry. No parameter is restated in prose.
RUN_BLENDER_PYTHON_SCHEMA = {
    "type": "function",
    "function": {
        "name": RUN_PYTHON,
        "description": (
            "Run Python inside the live Blender session and return its output as JSON. "
            "The code runs on Blender's main thread with the full bpy API and NO sandbox: "
            "it can read and change the user's scene, write files and reach the network. "
            f"A call is interrupted at {budget.CALL_SECONDS:.0f}s, and a turn may spend "
            f"{budget.TURN_SECONDS:.0f}s running code in total. The interrupt reaches "
            "pure Python, `time.sleep` and a blocked read; a single long Blender or "
            "NumPy call is only stopped once it returns, and code that catches the "
            "interrupt or disables the alarm cannot be stopped at all - so the user "
            "may have to force-quit Blender. Blender's UI is frozen while a call runs "
            "and nothing you print reaches it. Keep every call short and do one "
            "meaningful thing. `bpy`, `C` "
            "(bpy.context), `D` (bpy.data), `math` and `Vector`/`Matrix`/`Euler`/`Quaternion` "
            "are already bound, as in the Python Console. Check an operator's return value: "
            "a refused call returns {'CANCELLED'} silently. On failure the reply carries the "
            "full traceback and whatever was printed first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source, exec'd as-is.",
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "One imperative line naming what this call does and to which object, "
                        "1-80 characters, e.g. \"Scale Cube 1.3x on Z\". It labels the "
                        "transcript row and the undo step, and it is required: state the "
                        "intent before you mutate anything."
                    ),
                },
            },
            "required": ["code", "purpose"],
        },
    },
}

# The `guide` topic list is *generated* from the registry (ticket 10: the list the
# description carries "cannot drift"), so it is built here rather than typed.
GUIDE_LIST = guides.topic_list()

SCENE_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": SCENE_INFO,
        "description": (
            "Read the live scene and return JSON. The `summary` scope is the same object "
            "injected as this turn's live scene summary: authoritative for present-tense UI "
            "state as of capture, stale once you mutate - re-read rather than remember. Use "
            "`selection`/`active` to resolve \"this\"/\"it\"; use `objects` with `filter` for "
            "\"the camera\" or \"all lights\". There is deliberately no full-scene mode: a "
            "large scene comes back as counts plus a capped list, and `truncated` means "
            "narrow the filter, not page. `mesh_stats` evaluates the mesh and is the "
            "expensive flag; ask for it only for objects you need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": list(SCENE_SCOPES),
                    "description": (
                        "What to read. `summary` (the default) aggregates: counts, the "
                        "collection tree, the active object and the selection. `objects` "
                        "lists and can be filtered. `selection` and `active` are the two "
                        "narrow cases of that list."
                    ),
                },
                "filter": {
                    "type": "object",
                    "properties": {
                        "types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": 'Object types, e.g. ["MESH", "CAMERA"].',
                        },
                        "name_contains": {
                            "type": "string",
                            "description": "Case-insensitive substring of the object name.",
                        },
                        "collection": {
                            "type": "string",
                            "description": "Only objects linked into a collection with this name.",
                        },
                        "selected_only": {
                            "type": "boolean",
                            "description": "Only selected objects.",
                        },
                    },
                    "description": "Only for scope `objects`.",
                },
                "include": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(SCENE_INCLUDES)},
                    "description": (
                        "Per-object fields to return. Defaults: none for `summary`, "
                        "['transform'] for `selection`/`objects`, and "
                        "['transform','dimensions','materials','modifiers','mesh_stats'] "
                        "for `active`."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": SCENE_LIMIT_MAX,
                    "description": (
                        f"Most objects to list. {SCENE_LIMIT} by default, {SCENE_LIMIT_MAX} at "
                        "most. A larger request is capped and the result says so."
                    ),
                },
            },
        },
    },
}

RNA_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": RNA_INFO,
        "description": (
            "Look a Blender API name up in the live build, and the only sanctioned way to "
            "know one exists: existence is checked against a cached enumeration because "
            "`getattr` lies (it answers for names that are not there). Call this before "
            "writing any `bpy` name you are not certain of. `not_found` returns close "
            "matches rather than inventing an answer.\n\n"
            "kind: `type` | `operator` | `search` | `guide`. A `type` answer carries the "
            "type's properties (identifier, type, default, enum items, range) and its "
            "`bl_rna.functions`, filtered by `filter`. An `operator` answer carries the "
            "operator's properties, defaults and enum items plus `poll_now` - which is "
            "*this moment's* context, not a promise about the next call. `search` is a "
            "substring match over every registered operator id. `guide` returns long-tail "
            "idiom notes:\n"
            + GUIDE_LIST
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(RNA_KINDS),
                    "description": "What sort of name `name` is.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "The type name (`Object`, `Mesh`), the operator id "
                        "(`mesh.primitive_cube_add`), the `search` substring, or the guide "
                        f"topic ({', '.join(guides.TOPICS)})."
                    ),
                },
                "filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring; only properties and functions whose "
                        "identifier contains it come back. Use it: `Object` has "
                        f"{RNA_PROPERTY_LIMIT}+ properties and they do not all fit."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": RNA_LIMIT_MAX,
                    "description": (
                        f"Most entries to return: {RNA_PROPERTY_LIMIT} by default for "
                        f"`type`, {RNA_SEARCH_LIMIT} for `search`, {RNA_LIMIT_MAX} at most."
                    ),
                },
            },
            "required": ["kind", "name"],
        },
    },
}

SCHEMAS = [RUN_BLENDER_PYTHON_SCHEMA, SCENE_INFO_SCHEMA, RNA_INFO_SCHEMA]


def tool_names() -> list[str]:
    """The names the registry declares, in the order the model reads them."""
    return [schema["function"]["name"] for schema in SCHEMAS]


def schema_for(name: str) -> dict | None:
    for schema in SCHEMAS:
        if schema["function"]["name"] == name:
            return schema
    return None


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

# The elision marker, as a template so its own width can be reserved exactly:
# a cap that is reported but overshot by the width of the number reporting it is
# not a cap.
_ELISION = "\u2026 [{count} chars elided] \u2026"


def elide(text: str, cap: int, head_weight: float = 0.5) -> tuple[str, bool]:
    """Middle-elide `text` to at most `cap` characters.

    The marker is the literal from ticket 06 so the model can tell a short answer
    from a tiring one. `head_weight` 1.0 keeps only the head, 0.0 only the tail.
    """
    if len(text) <= cap:
        return text, False
    widest = _ELISION.format(count="9" * len(str(len(text))))
    budget = max(0, cap - len(widest))
    head = int(budget * head_weight)
    tail = budget - head
    marker = _ELISION.format(count=len(text) - head - tail)
    return text[:head] + marker + (text[len(text) - tail:] if tail else ""), True


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class _EofStdin:
    """Stdin that is always at EOF.

    `contextlib.redirect_stdin` does not exist (verified in ticket 06), and
    without this an `input()` in model code would block the main thread with no
    way to answer it - the GUI would simply stop.
    """

    encoding = "utf-8"

    def read(self, *args) -> str:
        return ""

    def readline(self, *args) -> str:
        return ""

    def readlines(self, *args) -> list:
        return []

    def __iter__(self):
        return iter(())

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no stdin is available while the model's code runs")

    def close(self) -> None:
        pass


def _model_line(error: BaseException) -> int | None:
    """The line of the model's code that raised, if it can be told.

    `error.__traceback__.tb_lineno` alone is the *outer* exec call site rather
    than the model's line (verified in ticket 06), so walk the traceback for the
    last frame compiled from `<model>`. A `SyntaxError` has no traceback and
    carries `lineno` itself.
    """
    if isinstance(error, SyntaxError):
        return error.lineno
    line = None
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == FILENAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def run_python(code: str, purpose: str, bindings: dict, limits=None) -> dict:
    """Exec `code` with a fresh namespace and return the envelope.

    Never raises on the model's behalf: anything the code throws - including
    `SystemExit` and `KeyboardInterrupt` - is caught as `BaseException` and
    reported, because a raised exception here would take the loop with it.

    **The budget is opened here and nowhere else**, because this is the only place
    that knows exactly which block of work is the model's: the window covers
    `exec` and nothing after it, so the elision, the JSON and the loop's own
    bookkeeping can never be interrupted by an alarm that was meant for the code.
    A raise that lands in this function's own body would be a bug in the budget
    rather than a stop, and the narrow window is what makes that structurally
    impossible instead of merely unlikely.

    `limits` defaults to the process's one instance (`budget.LIMITS`), which is the
    same object the loop opens the turn on; the checks pass their own.
    """
    limits = limits if limits is not None else budget.LIMITS
    stdout = io.StringIO()
    stderr = io.StringIO()
    truncated = False
    note = None

    module = types.ModuleType("__main__")
    module.__dict__.update(bindings)
    previous_main = sys.modules.get("__main__")
    previous_stdin = sys.stdin
    error = None
    stopped = None
    try:
        # `compile` separately, so a SyntaxError carries lineno/offset.
        compiled = compile(code, FILENAME, "exec")
        sys.modules["__main__"] = module
        sys.stdin = _EofStdin()
        with limits.call():
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exec(compiled, module.__dict__)
    except budget.Exceeded as exc:
        # The budget, and not a failure of the code. It is caught here rather than
        # allowed to escape because the loop's contract with the tool layer is
        # "a result, never an exception" - and because catching it here is what
        # turns it into a sentence the model can read on the next turn.
        stopped = exc
    except BaseException as exc:  # noqa: BLE001 - the whole point is to report it
        error = exc
    finally:
        sys.stdin = previous_stdin
        if previous_main is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = previous_main

    out_text, out_cut = elide(stdout.getvalue(), STDOUT_CAP, head_weight=0.5)
    err_text, err_cut = elide(stderr.getvalue(), STDERR_CAP, head_weight=0.0)
    truncated = out_cut or err_cut

    verdict = limits.verdict()
    if budget.stopped(verdict):
        # Two different worlds behind one condition, and the verdict says which:
        # the call was unwound by the interrupt, or the code ignored it and came
        # back anyway. Both belong in the envelope, because both mean the call did
        # not run under the bound it was supposed to.
        note = budget.verdict_note(verdict)

    if stopped is not None:
        formatted = "".join(
            traceback.format_exception(type(stopped), stopped, stopped.__traceback__)
        ).strip()
        detail = {
            "kind": "budget",
            "budget_kind": stopped.kind,
            "seconds": round(stopped.seconds, 3),
            "limit": round(stopped.limit, 3),
            "interrupts": stopped.interrupts,
            "late": verdict.get("late", False),
            "message": str(stopped),
            "traceback": elide(formatted, TRACEBACK_CAP, head_weight=0.0)[0],
        }
        if out_text:
            detail["stdout_before_error"] = out_text
        envelope = {
            "ok": False,
            "tool": RUN_PYTHON,
            "summary": f"{purpose} \u2014 stopped by its {stopped.limit:.0f}s budget",
            "status": "error",
            "purpose": purpose,
            "stdout": out_text,
            "stderr": err_text,
            "truncated": truncated,
            "note": note,
            "error": detail,
            "budget": verdict,
        }
        return _result(envelope)

    if error is None:
        envelope = {
            "ok": True,
            "tool": RUN_PYTHON,
            "summary": f"{purpose} \u2014 ok",
            "status": "ok",
            "purpose": purpose,
            "stdout": out_text,
            "stderr": err_text,
            "truncated": truncated,
            "note": note,
            "error": None,
        }
        if note:
            envelope["budget"] = verdict
        return _result(envelope)

    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    trace_text, trace_cut = elide(formatted, TRACEBACK_CAP, head_weight=0.0)
    if trace_cut:
        truncated = True
    # Bpy report text appears both in stderr and inside the RuntimeError message;
    # ticket 06 says accept the duplication rather than string-dedupe it.
    detail = {
        "kind": "exec_error",
        "type": type(error).__name__,
        "message": str(error)[:1000],
        "line": _model_line(error),
        "traceback": trace_text,
    }
    if out_text:
        detail["stdout_before_error"] = out_text
    envelope = {
        "ok": False,
        "tool": RUN_PYTHON,
        "summary": f"{purpose} \u2014 {type(error).__name__}: {str(error)[:120]}",
        "status": "error",
        "purpose": purpose,
        "stdout": out_text,
        "stderr": err_text,
        "truncated": truncated,
        "note": note,
        "error": detail,
    }
    if note:
        # The code raised, but only after ignoring an interrupt - the budget ran
        # out and the failure is downstream of that. Both facts travel.
        envelope["budget"] = verdict
    return _result(envelope)


def _result(envelope: dict) -> dict:
    """The envelope, plus the two renderings the loop passes around opaquely."""
    if envelope.get("tool") in (SCENE_INFO, RNA_INFO):
        envelope = _fit_by_dropping(envelope)

    content = json.dumps(envelope, ensure_ascii=False, default=str)
    if len(content) > RESULT_CAP:
        # The shared cap, as a last resort, and only for `run_blender_python`: the
        # per-field caps above normally keep this from firing at all, and a
        # traceback plus prints has no entries to drop. The elided string is no
        # longer valid JSON, and that is the honest trade - an unreadable tail
        # beats a blown context window, and the flag says so.
        elided, _ = elide(content, RESULT_CAP)
        envelope = dict(envelope)
        envelope["truncated"] = True
        # An existing note is *kept*, not overwritten: the read-only tools use it
        # to name the narrowing lever, and that is still the useful sentence after
        # the result itself has also been elided.
        existing = envelope.get("note")
        capped = f"result elided to {RESULT_CAP} chars"
        envelope["note"] = f"{existing}; {capped}" if existing else capped
        content = elided
    return {
        "ok": bool(envelope.get("ok")),
        "envelope": envelope,
        "content": content,
        "detail": json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
        "summary": envelope.get("summary") or "",
    }


# The list fields the shared cap may drop entries from, and the envelope field each
# one's count is reported in (None where there is no such field).
_DROPPABLE = (
    ("properties", "returned"),
    ("functions", None),
    ("objects", "returned"),
    ("operators", "returned"),
    ("collection_tree", None),
)


def _fit_by_dropping(envelope: dict) -> dict:
    """Bring a read-only result under the shared cap by dropping trailing entries.

    Ticket 06 asks for exactly this - "the shared 8,000-char cap drops trailing
    entries and reports" - and why it matters is the whole point of a read-only
    tool: the model *parses* this answer. A character-elided JSON string is not
    parseable, so the honest fallback for these two tools is fewer entries with
    `truncated` and a `note`, not a broken document. The longest list is halved
    repeatedly, so the cost is a handful of serialisations rather than one per
    entry.

    `run_blender_python` is deliberately not routed through here: its result is a
    traceback and some prints, which have no entries to drop.
    """
    text = json.dumps(envelope, ensure_ascii=False, default=str)
    if len(text) <= RESULT_CAP:
        return envelope

    drops: list[str] = []
    for _ in range(24):
        longest = None
        for key, _count in _DROPPABLE:
            value = envelope.get(key)
            if isinstance(value, list) and len(value) > 1:
                if longest is None or len(value) > len(envelope[longest]):
                    longest = key
        if longest is None:
            break

        envelope = dict(envelope)
        kept = envelope[longest][: max(1, len(envelope[longest]) // 2)]
        envelope[longest] = kept
        envelope["truncated"] = True
        drops.append(f"{longest} {len(kept)}")
        for key, count_field in _DROPPABLE:
            if key == longest and count_field:
                envelope[count_field] = len(kept)
        text = json.dumps(envelope, ensure_ascii=False, default=str)
        if len(text) <= RESULT_CAP:
            break

    if drops:
        # One sentence, not one per halving: the model needs the final shape, and
        # a note that grew a clause per pass would itself eat the budget.
        existing = envelope.get("note")
        cut = f"over {RESULT_CAP} chars, so lists were cut to: {', '.join(drops)}"
        envelope["note"] = f"{existing} {cut}" if existing else cut
    return envelope


# ---------------------------------------------------------------------------
# Reading the world safely
# ---------------------------------------------------------------------------
#
# Every read goes through one of these, and none of them may raise. A read that
# fails is a *result* - the same rule the sandbox follows - because a tool that
# raised would take the loop with it and a turn that dies on a bad attribute name
# is worse than a turn that says "that is not there".


def _text(value, cap: int = 0) -> str:
    """A string from RNA, whatever it actually was.

    RNA string properties come back as `None` when unset and descriptions run to
    paragraphs, so every one of them goes through here. A cap elides the *tail*,
    with a marker, so a truncated description does not read as a complete one.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if cap and len(text) > cap:
        text = text[: cap - 1].rstrip() + "\u2026"
    return text


def _vector(value) -> list | None:
    """A 3-vector as JSON, or None when it is not one."""
    try:
        return [round(float(component), 6) for component in value]
    except (TypeError, ValueError, AttributeError):
        return None


def _call(world, method: str, *args):
    """One world read, or None. The world is live RNA; it is allowed to decline."""
    reader = getattr(world, method, None)
    if not callable(reader):
        return None
    try:
        return reader(*args)
    except Exception:  # noqa: BLE001 - reported as an absence, never raised
        return None


def _world_names(world, method: str) -> list[str]:
    value = _call(world, method)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _objects(world) -> list:
    """Every object in the file, sorted by name.

    Sorted because ticket 06 makes ordering part of the contract ("objects sorted
    by name, properties in RNA order"): a list that reorders between two identical
    calls reads as a change when nothing changed.
    """
    value = _call(world, "objects")
    if not isinstance(value, (list, tuple)):
        return []
    return sorted(value, key=lambda obj: _text(getattr(obj, "name", "")))


def _close_matches(name: str, candidates: list[str], limit: int = MATCH_LIMIT) -> list[str]:
    """5-10 plausible spellings for a name that is not there (ticket 06).

    Two different failure modes need two different answers: `mesh.primitive_cube`
    is a truncation, `Object.IsVisble` is a typo. Substring tiers come first and
    `difflib` only fills what is left, because a near miss and a prefix are not
    the same kind of evidence.

    Within a tier, **shorter names come first**, which is not cosmetic: asking for
    `Obj` against the real 5.2.2 build otherwise returns eight
    `OBJECT_MT_light_linking_context_menu`-shaped menu ids before it reaches
    `Object`, and the answer the model actually needed was crowded out by names
    that merely sort earlier. Nothing here invents a name: every entry exists.
    """
    needle = _text(name).strip().lower()
    if not needle or not candidates:
        return []

    def tier(test) -> list[str]:
        return sorted((item for item in candidates if test(item)), key=lambda item: (len(item), item))

    found = tier(lambda item: item.lower() == needle)
    found += [item for item in tier(lambda item: item.lower().startswith(needle)) if item not in found]
    found += [
        item for item in tier(lambda item: needle in item.lower()) if item not in found
    ]
    if len(found) < limit:
        for item in difflib.get_close_matches(needle, candidates, n=limit, cutoff=0.55):
            if item not in found:
                found.append(item)
    return found[:limit]


def _not_found(tool: str, message: str, matches: list[str] | None = None,
               hint: str = "") -> dict:
    """`not_found` is the anti-hallucination feature (ticket 06 §3).

    It never guesses an answer: it names what was asked for and hands back what
    actually exists. `matches` and `hint` sit beside the message because that is
    what the model needs to fix its own call in one round.
    """
    error: dict = {"kind": "not_found", "message": message}
    if matches:
        error["matches"] = list(matches)
    if hint:
        error["hint"] = hint
    return _result({"ok": False, "tool": tool, "summary": message, "error": error})


def _invalid_kind(kind, valid: tuple) -> dict:
    """Ticket 06's `kind` enum has this member; this is its only producer.

    A wrong `kind` is not a wrong *name*, and saying so separately is what stops
    the model concluding the thing does not exist when it only asked the wrong
    question.
    """
    message = f"`kind` must be one of {', '.join(valid)} - got {kind!r}."
    return _result({
        "ok": False,
        "tool": RNA_INFO,
        "summary": message,
        "error": {"kind": "invalid_kind", "message": message},
    })


def _limit(value, default: int, maximum: int) -> tuple[int, str]:
    """A clamped list budget, and a sentence about it.

    Clamped rather than refused, deliberately: the build ticket's own wording is
    "asking for more than the cap returns the cap and says it was truncated", and
    an argument error would refuse the request instead of answering it. A limit is
    a budget, not a fact about the scene, so the fallback is the default and the
    note says which one was used - the one thing that would be dishonest here is
    applying a different number silently.
    """
    if value is None:
        return default, ""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        return default, f"limit was not a whole number, so the default {default} was used."
    if value < 1:
        return 1, "limit was below 1 and was raised to 1."
    if value > maximum:
        return maximum, f"limit {value} is above the {maximum} maximum and was capped to it."
    return value, ""


# ---------------------------------------------------------------------------
# get_scene_info
# ---------------------------------------------------------------------------

SUMMARY_SCHEMA = 1

_CUSTOM_PROP_SKIP = ("_RNA_UI",)


def _collection_tree(world) -> tuple[list, bool]:
    """The scene's collections, bounded to COLLECTION_TREE_CAP nodes.

    Bounded because the summary's job is to *describe* the scene, not to enumerate
    it: a file with four hundred collections would otherwise blow the shared cap
    and arrive as unreadable JSON. The boolean travels with the tree so a cut tree
    cannot be mistaken for a small one.
    """
    tree = _call(world, "collections")
    if not isinstance(tree, (list, tuple)):
        return [], False

    budget = [COLLECTION_TREE_CAP]
    cut = False

    def node(entry) -> dict | None:
        nonlocal cut
        if budget[0] <= 0:
            cut = True
            return None
        budget[0] -= 1
        children = []
        for child in getattr(entry, "get", lambda *_: None)("children", []) or []:
            built = node(child) if isinstance(child, dict) else None
            if built is not None:
                children.append(built)
        return {
            "name": _text(_get(entry, "name")),
            "count": _get(entry, "count"),
            "children": children,
        }

    out = []
    for entry in tree:
        if not isinstance(entry, dict):
            continue
        built = node(entry)
        if built is not None:
            out.append(built)
    return out, cut


def _get(mapping, key, default=None):
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    return default


def scene_summary(world, captured: str = "turn_start") -> dict:
    """Ticket 06's `summary` scope: counts, never a list.

    This is *the* definition - the injected live summary is this dict serialised,
    and the tool's `summary` scope is this dict in an envelope - so the two cannot
    disagree. `captured` differs because the two are captured at different
    moments: `turn_start` for the message that rides along with a request,
    `tool_call` for one asked for mid-turn.
    """
    facts = _call(world, "facts")
    if not isinstance(facts, dict):
        facts = {}
    objects = _objects(world)

    counts: dict[str, int] = {}
    for obj in objects:
        key = _text(getattr(obj, "type", None)) or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1

    selected = _world_names(world, "selected_names")
    active_name = _text(_call(world, "active_name")) or None
    active = next((obj for obj in objects if _text(getattr(obj, "name", "")) == active_name), None)
    active_type = _text(getattr(active, "type", None)) or None if active is not None else None
    tree, tree_cut = _collection_tree(world)

    return {
        "scene": facts.get("scene"),
        "filepath": facts.get("filepath"),
        "is_saved": bool(facts.get("is_saved", False)),
        "is_dirty": bool(facts.get("is_dirty", False)),
        "mode": facts.get("mode"),
        "frame": facts.get("frame"),
        "frame_range": facts.get("frame_range"),
        "render_engine": facts.get("render_engine"),
        "unit_system": facts.get("unit_system"),
        "global_undo": facts.get("global_undo"),
        "object_count": len(objects),
        "collection_tree": tree,
        "collection_tree_truncated": tree_cut,
        "object_type_counts": counts,
        "selection": {"count": len(selected), "names": selected[:SELECTION_NAME_CAP]},
        "active": {"name": active_name, "type": active_type},
        "captured": captured,
        "schema": SUMMARY_SCHEMA,
    }


def _summary_line(fields: dict) -> str:
    """One plain-text line for the panel. The model reads the JSON, not this."""
    types = ", ".join(f"{key} {value}" for key, value in sorted(fields["object_type_counts"].items()))
    active = fields["active"]["name"] or "none"
    return (
        f"{fields['scene'] or 'scene'}: {fields['object_count']} objects"
        + (f" ({types})" if types else "")
        + f", mode {fields['mode']}, active {active}, "
        + f"selection {fields['selection']['count']}"
    )


def _default(prop):
    """A property's default, in JSON-safe form.

    `prop.default` is the one RNA read that can be an array rather than a scalar,
    and a big one (`matrix_world`), so it is bounded here rather than at the cap.
    """
    try:
        value = prop.default
    except Exception:  # noqa: BLE001 - RNA can decline to have a default
        return None
    try:
        if isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return _text(value, 120)
        if hasattr(value, "__len__") and not isinstance(value, dict):
            return [round(float(item), 6) for item in value][:64]
    except (TypeError, ValueError):
        pass
    return _text(value, 120)


def _property_entry(prop) -> dict:
    """One RNA property, with only the fields ticket 06 §3 asks for."""
    entry = {
        "id": _text(getattr(prop, "identifier", "")),
        "type": _text(getattr(prop, "type", "")),
        "readonly": bool(getattr(prop, "is_readonly", False)),
        "default": _default(prop),
    }
    length = getattr(prop, "array_length", 0)
    if isinstance(length, int) and length > 1:
        entry["array_length"] = length
    items = getattr(prop, "enum_items", None)
    if items:
        # `enum_count` is always present when the list is cut, so a short list
        # cannot be read as the whole one (ticket 06 §3).
        entry["enum_items"] = [_text(getattr(item, "identifier", "")) for item in items][:ENUM_ITEM_CAP]
        entry["enum_count"] = len(items)
    low, high = getattr(prop, "hard_min", None), getattr(prop, "hard_max", None)
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > float("-inf") and high < float("inf"):
        entry["range"] = [low, high]
    subtype = _text(getattr(prop, "subtype", ""))
    if subtype:
        entry["subtype"] = subtype
    description = _text(getattr(prop, "description", ""), DESCRIPTION_CAP)
    if description:
        entry["description"] = description
    return entry


def _function_entry(function) -> dict:
    parameters = []
    for parameter in getattr(function, "parameters", None) or []:
        parameters.append({
            "id": _text(getattr(parameter, "identifier", "")),
            "type": _text(getattr(parameter, "type", "")),
        })
    return {
        "id": _text(getattr(function, "identifier", "")),
        "parameters": parameters,
        "description": _text(getattr(function, "description", ""), DESCRIPTION_CAP),
    }


def _structured_entry_list(rna, attribute: str, filter_text: str, limit: int, build) -> tuple[list, int, bool, str]:
    """Filter, cap and describe one of an RNA struct's entry lists.

    Shared by `properties` and `functions` because the three questions - how many
    are there, how many matched, how many fit - have the same answer shape in both
    cases, and answering them twice is how the two drift apart.
    """
    entries = list(getattr(rna, attribute, None) or [])
    total = len(entries)
    matched = entries
    if filter_text:
        needle = filter_text.lower()
        matched = [
            entry for entry in entries
            if needle in _text(getattr(entry, "identifier", "")).lower()
        ]
    kept = matched[:limit]
    truncated = len(matched) > len(kept)
    note = ""
    if truncated:
        note = (
            f"{len(matched)} matched, {len(kept)} returned; raise limit "
            f"(max {RNA_LIMIT_MAX}) or narrow filter."
        )
    elif filter_text and len(matched) < total:
        note = f"filter {filter_text!r} matched {len(matched)} of {total}."
    return [build(entry) for entry in kept], total, truncated, note


def _base_chain(rna) -> str:
    """The base chain, walked via `.base`.

    `Object.bl_rna.base` is a single struct rather than a list (verified in
    ticket 06), so this is a walk, not an iteration - and it is bounded in case a
    future build ever makes it a cycle.
    """
    names: list[str] = []
    node = getattr(rna, "base", None)
    while node is not None and len(names) < 10:
        names.append(_text(getattr(node, "identifier", "")))
        node = getattr(node, "base", None)
    return " > ".join(name for name in names if name)


def _object_entry(obj, include: tuple) -> dict:
    """One object, with the requested fields. Nothing here may raise."""
    entry: dict = {
        "name": _text(getattr(obj, "name", "")),
        "type": _text(getattr(obj, "type", "")),
    }

    if "transform" in include:
        entry["transform"] = {
            "location": _vector(getattr(obj, "location", None)),
            "rotation_mode": _text(getattr(obj, "rotation_mode", "")) or None,
            "rotation_euler": _vector(getattr(obj, "rotation_euler", None)),
            "scale": _vector(getattr(obj, "scale", None)),
        }
    if "dimensions" in include:
        entry["dimensions"] = _vector(getattr(obj, "dimensions", None))
    if "parent" in include:
        parent = getattr(obj, "parent", None)
        entry["parent"] = _text(getattr(parent, "name", "")) or None
    if "collections" in include:
        linked = getattr(obj, "users_collection", None) or []
        names = sorted(_text(getattr(item, "name", "")) for item in linked)
        entry["collections"] = names[:NAME_LIST_CAP]
        if len(names) > NAME_LIST_CAP:
            entry["collection_count"] = len(names)
    if "materials" in include:
        slots = getattr(obj, "material_slots", None) or []
        entry["materials"] = [
            _text(getattr(getattr(slot, "material", None), "name", "")) or None for slot in slots
        ]
    if "modifiers" in include:
        entry["modifiers"] = [
            {"name": _text(getattr(mod, "name", "")), "type": _text(getattr(mod, "type", ""))}
            for mod in (getattr(obj, "modifiers", None) or [])
        ]
    if "constraints" in include:
        entry["constraints"] = [
            {"name": _text(getattr(con, "name", "")), "type": _text(getattr(con, "type", ""))}
            for con in (getattr(obj, "constraints", None) or [])
        ]
    if "custom_properties" in include:
        keys = sorted(
            key for key in _keys(obj)
            if isinstance(key, str) and not key.startswith("_") and key not in _CUSTOM_PROP_SKIP
        )
        entry["custom_properties"] = {
            key: _text(_index(obj, key), CUSTOM_PROP_VALUE_CAP) for key in keys[:CUSTOM_PROP_CAP]
        }
        # The count always travels with the dict, so a short dict cannot be read
        # as "this object has three custom properties".
        entry["custom_property_count"] = len(keys)
    if "mesh_stats" in include:
        entry["mesh_stats"] = _mesh_stats(obj)
    return entry


def _keys(obj) -> list:
    try:
        return list(obj.keys())
    except Exception:  # noqa: BLE001
        return []


def _index(obj, key):
    try:
        return obj[key]
    except Exception:  # noqa: BLE001
        return None


def _mesh_stats(obj) -> dict | None:
    """Evaluated mesh counts, for meshes only.

    Ticket 06 documents this as the expensive flag: it evaluates the mesh, which
    is why it is computed for the returned objects only and never for a list that
    was cut by the cap.
    """
    if _text(getattr(obj, "type", "")) != "MESH":
        return None
    data = getattr(obj, "data", None)
    if data is None:
        return None
    stats: dict = {}
    for field, attribute in (
        ("vertices", "vertices"), ("edges", "edges"), ("polygons", "polygons"),
    ):
        try:
            stats[field] = len(getattr(data, attribute))
        except Exception:  # noqa: BLE001
            stats[field] = None
    try:
        stats["material_slots"] = len(getattr(obj, "material_slots", []) or [])
    except Exception:  # noqa: BLE001
        stats["material_slots"] = None
    return stats


def _wants(filters: dict, key: str):
    value = filters.get(key)
    return value if value not in (None, "", [], {}) else None


def _matches(obj, filters: dict, selected: set) -> bool:
    """The three filters of ticket 06 §2, plus `selected_only`."""
    types = _wants(filters, "types")
    if types is not None:
        wanted = types if isinstance(types, (list, tuple)) else [types]
        wanted = {_text(item).strip().upper() for item in wanted}
        if _text(getattr(obj, "type", "")).upper() not in wanted:
            return False
    needle = _wants(filters, "name_contains")
    if needle is not None and _text(needle).lower() not in _text(getattr(obj, "name", "")).lower():
        return False
    collection = _wants(filters, "collection")
    if collection is not None:
        names = {_text(getattr(item, "name", "")) for item in (getattr(obj, "users_collection", None) or [])}
        if _text(collection) not in names:
            return False
    if filters.get("selected_only") and _text(getattr(obj, "name", "")) not in selected:
        return False
    return True


def scene_info(arguments: dict, world) -> dict:
    """Ticket 06 §2: a bounded answer, never a dump.

    The one thing this tool must not do is enumerate the scene. `summary` answers
    "what is in my file" with counts, `objects` answers "the things named X" with a
    capped list and a `truncated` flag whose `note` names the narrowing lever, and
    there is deliberately no mode that returns everything.
    """
    if world is None:
        return _argument_error(
            SCENE_INFO, "No Blender session is attached to this call, so nothing could be read."
        )

    scope = arguments.get("scope")
    if scope is None:
        scope = "summary"
    if not isinstance(scope, str) or scope not in SCENE_SCOPES:
        return _argument_error(
            SCENE_INFO,
            f"`scope` must be one of {', '.join(SCENE_SCOPES)} - got {scope!r}.",
        )

    filters = arguments.get("filter")
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        return _argument_error(SCENE_INFO, "`filter` must be an object.")
    if filters and scope != "objects":
        return _argument_error(
            SCENE_INFO,
            f"`filter` only applies to the `objects` scope, not `{scope}`.",
            hint="Use scope=\"objects\" to filter, or drop the filter.",
        )
    unknown = sorted(key for key in filters if key not in SCENE_FILTER_KEYS)
    if unknown:
        return _argument_error(
            SCENE_INFO,
            f"Unknown filter field(s) {', '.join(unknown)}.",
            hint=f"`filter` takes {', '.join(SCENE_FILTER_KEYS)}.",
        )

    include_argument = arguments.get("include")
    if include_argument is None:
        include = SCENE_INCLUDE_DEFAULTS[scope]
    elif isinstance(include_argument, (list, tuple)) and all(
        isinstance(item, str) for item in include_argument
    ):
        unknown_includes = sorted({item for item in include_argument if item not in SCENE_INCLUDES})
        if unknown_includes:
            return _argument_error(
                SCENE_INFO,
                f"Unknown include flag(s) {', '.join(unknown_includes)}.",
                hint=f"`include` takes {', '.join(SCENE_INCLUDES)}.",
            )
        include = tuple(dict.fromkeys(include_argument))
    else:
        return _argument_error(
            SCENE_INFO, "`include` must be a list of include flag strings.",
            hint=f"`include` takes {', '.join(SCENE_INCLUDES)}.",
        )

    limit, limit_note = _limit(arguments.get("limit"), SCENE_LIMIT, SCENE_LIMIT_MAX)

    if scope == "summary":
        fields = scene_summary(world, captured="tool_call")
        return _result({
            "ok": True,
            "tool": SCENE_INFO,
            "summary": _summary_line(fields),
            **fields,
        })

    objects = _objects(world)
    selected = set(_world_names(world, "selected_names"))

    if scope == "active":
        active_name = _text(_call(world, "active_name")) or None
        matched = [obj for obj in objects if _text(getattr(obj, "name", "")) == active_name]
        if not matched:
            return _not_found(
                SCENE_INFO,
                "There is no active object.",
                hint="The `summary` scope reports `active: {\"name\": null}` in this state.",
            )
    elif scope == "selection":
        matched = [
            obj for obj in objects
            if _text(getattr(obj, "name", "")) in selected and _matches(obj, filters, selected)
        ]
    else:
        matched = [obj for obj in objects if _matches(obj, filters, selected)]

    returned = matched[:limit]
    truncated = len(matched) > len(returned)
    note = ""
    if truncated:
        note = (
            f"{len(matched)} matched, {len(returned)} returned; narrow with "
            "filter.name_contains or types"
        )
    if limit_note:
        note = f"{limit_note} {note}".strip()

    return _result({
        "ok": True,
        "tool": SCENE_INFO,
        "summary": f"{scope}: {len(returned)} of {len(matched)} objects"
        + (" (truncated)" if truncated else ""),
        "scope": scope,
        "matched": len(matched),
        "returned": len(returned),
        "truncated": truncated,
        "note": note or None,
        # `mesh_stats` is computed only for the objects actually returned, which
        # is what makes it affordable on a large scene (ticket 06 §2).
        "objects": [_object_entry(obj, include) for obj in returned],
    })


# ---------------------------------------------------------------------------
# get_rna_info
# ---------------------------------------------------------------------------

def _kind_hint(world, kind: str, name: str) -> str:
    """A wrong-`kind` call earns a hint naming the right one (ticket 06 §3)."""
    if kind != "operator" and name in _world_names(world, "operator_names"):
        return f"{name!r} is an operator id; ask again with kind=\"operator\"."
    if kind != "type" and name in _world_names(world, "type_names"):
        return f"{name!r} is a type; ask again with kind=\"type\"."
    return ""


def _guide_info(name: str) -> dict:
    guide = guides.get(name)
    if guide is None:
        return _not_found(
            RNA_INFO,
            f"There is no guide named {name!r}.",
            matches=list(guides.TOPICS),
            hint="`kind=\"guide\"` takes one of the topics listed in this tool's description.",
        )
    return _result({
        "ok": True,
        "tool": RNA_INFO,
        "summary": f"guide {name}",
        "kind": "guide",
        "found": True,
        "topic": name,
        "summary_text": guide["summary"],
        "body": guide["body"],
        "guides_version": guides.GUIDES_VERSION,
    })


def rna_info(arguments: dict, world) -> dict:
    """Ticket 06 §3: the minimum that stops a model hallucinating `bpy`.

    Exact-name lookup against the live build, in four shapes: what a type is, what
    an operator takes, which operators exist, and the long-tail guides. Existence
    is answered from the build's own enumeration rather than `getattr`, because
    `getattr(bpy.ops.mesh, "frobnicate")` returns a function (measured, ticket 06).
    """
    kind = arguments.get("kind")
    if not isinstance(kind, str) or kind not in RNA_KINDS:
        return _invalid_kind(kind, RNA_KINDS)

    name = arguments.get("name")
    if not isinstance(name, str) or not name.strip():
        return _argument_error(
            RNA_INFO, "`name` is required: a type name, an operator id, a search substring, or a guide topic."
        )
    name = name.strip()

    filter_text = arguments.get("filter")
    if filter_text is not None and not isinstance(filter_text, str):
        return _argument_error(RNA_INFO, "`filter` must be a string.")
    filter_text = (filter_text or "").strip()

    if kind == "guide":
        return _guide_info(name)

    if kind == "search":
        limit, limit_note = _limit(arguments.get("limit"), RNA_SEARCH_LIMIT, RNA_LIMIT_MAX)
        candidates = _world_names(world, "operator_names")
        needle = name.lower()
        found = [item for item in candidates if needle in item.lower()]
        returned = found[:limit]
        truncated = len(found) > len(returned)
        note = ""
        if truncated:
            note = f"{len(found)} matched, {len(returned)} returned; give a longer pattern."
        if limit_note:
            note = f"{limit_note} {note}".strip()
        return _result({
            "ok": True,
            "tool": RNA_INFO,
            "summary": f"search {name!r}: {len(found)} operators",
            "kind": "search",
            "pattern": name,
            "total_matches": len(found),
            "returned": len(returned),
            "truncated": truncated,
            "note": note or None,
            "operators": returned,
        })

    limit_default = RNA_PROPERTY_LIMIT
    limit, limit_note = _limit(arguments.get("limit"), limit_default, RNA_LIMIT_MAX)
    operator_names = _world_names(world, "operator_names")

    if kind == "type":
        rna = _call(world, "rna_of_type", name)
        if rna is None:
            return _not_found(
                RNA_INFO,
                f"No type named {name!r} in this Blender build.",
                matches=_close_matches(name, _world_names(world, "type_names")),
                hint=_kind_hint(world, kind, name),
            )
        properties, total, truncated, note = _structured_entry_list(
            rna, "properties", filter_text, limit, _property_entry
        )
        functions, function_total, functions_cut, function_note = _structured_entry_list(
            rna, "functions", filter_text, limit, _function_entry
        )
        notes = " ".join(part for part in (limit_note, note or function_note) if part)
        return _result({
            "ok": True,
            "tool": RNA_INFO,
            "summary": f"type {name}: {len(properties)} of {total} properties",
            "kind": "type",
            "found": True,
            "id": _text(getattr(rna, "identifier", "")) or name,
            "name": _text(getattr(rna, "name", "")) or name,
            "description": _text(getattr(rna, "description", ""), DESCRIPTION_CAP),
            "base": _base_chain(rna),
            "property_count": total,
            "returned": len(properties),
            "truncated": truncated or functions_cut,
            "note": notes or None,
            "properties": properties,
            "functions": functions,
            "function_count": function_total,
        })

    rna = _call(world, "rna_of_operator", name)
    if rna is None or name not in operator_names:
        return _not_found(
            RNA_INFO,
            f"No operator with id {name!r} in this Blender build.",
            matches=_close_matches(name, operator_names),
            hint=_kind_hint(world, kind, name),
        )
    properties, total, truncated, note = _structured_entry_list(
        rna, "properties", filter_text, limit, _property_entry
    )
    poll = _call(world, "operator_poll", name)
    poll_now = bool(poll) if isinstance(poll, bool) else None
    return _result({
        "ok": True,
        "tool": RNA_INFO,
        "summary": f"operator {name}: {len(properties)} of {total} properties",
        "kind": "operator",
        "found": True,
        "id": name,
        "name": _text(getattr(rna, "name", "")) or name,
        "description": _text(getattr(rna, "description", ""), DESCRIPTION_CAP),
        "poll_now": poll_now,
        "poll_note": (
            "Whether this operator's poll passed in this call's context. It is not a "
            "promise about the next call: context changes, and a passed poll can still "
            "return {'CANCELLED'}."
        ),
        "property_count": total,
        "returned": len(properties),
        "truncated": truncated,
        "note": " ".join(part for part in (limit_note, note) if part) or None,
        "properties": properties,
    })


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _argument_error(tool: str, message: str, raw: str = "", hint: str = "") -> dict:
    error = {"kind": "tool_argument_error", "message": message}
    if raw:
        error["raw"] = raw[:RAW_ARGS_CAP]
    if hint:
        error["hint"] = hint
    envelope = {
        "ok": False,
        "tool": tool,
        "summary": f"{tool} \u2014 {message}",
        "status": "error",
        "error": error,
    }
    return _result(envelope)


def parse_arguments(call: dict) -> dict | None:
    """The call's `arguments`, as a dict, or None when they are not parseable.

    Measured (ticket 16, and AGENTS.md): the provider sends
    `tool_calls[].function.arguments` as a **JSON string, not an object**, and the
    worker concatenates the fragments the SSE deltas carried. So this is the first
    place the string is parsed, and a failure here is a normal outcome with a
    defined answer (ticket 09 §6): feed the raw text back and let the model fix
    it. A provider that ever sends an object is accepted rather than punished.
    """
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def prelude(world) -> dict:
    """The prelude dict for model code, from whatever the caller supplied.

    Three forms are accepted, deliberately. `toolbox.LiveWorld` is asked for
    `bindings()`; a bare callable *is* the prelude provider - which is what the
    sandbox's own checks pass, because the prelude is the only thing
    `run_blender_python` needs from Blender, and a fake there is the whole point of
    keeping the sandbox bpy-free; and a bare **dict** is the prelude itself, which
    is what `tools/loop_wire_probe.py` passes.

    The dict form used to fall through the `callable` test and come back empty, so
    the model's code ran against an empty namespace and died on its first name -
    seven wire-probe checks failed on that, and the suite missed it because the
    suite happens to pass a callable.
    """
    provider = getattr(world, "bindings", None) or world
    if isinstance(provider, dict):
        return provider
    if not callable(provider):
        return {}
    value = provider()
    return value if isinstance(value, dict) else {}


def execute_tool(call: dict, world=None, limits=None) -> dict:
    """Run one wire tool call. Never raises; always returns the loop's shape.

    `world` is the bpy side: `toolbox.LiveWorld` in Blender, a fake under
    `tests/test_conversation.py`. It is read lazily - `prelude()` only on a
    `run_blender_python` call - so dispatching costs nothing until something is
    actually run or read.

    `limits` is the budget seam: the two reader tools cannot run arbitrary code,
    so the only call that opens a budget window is the one that execs.
    """
    function = call.get("function") or {}
    name = function.get("name") or ""
    if name not in (RUN_PYTHON, SCENE_INFO, RNA_INFO):
        # Ticket 09 §6: an unknown or empty tool name is a `tool_argument_error`
        # naming it, and nothing is executed.
        return _argument_error(
            name or "(none)",
            f"Unknown tool {name!r}. The tools available are {', '.join(tool_names())}.",
        )

    raw = function.get("arguments")
    arguments = parse_arguments(call)
    if arguments is None:
        return _argument_error(
            name,
            "The arguments were not a JSON object. Send them as a JSON object.",
            raw=raw if isinstance(raw, str) else json.dumps(raw, default=str),
        )

    if name == SCENE_INFO:
        return scene_info(arguments, world)
    if name == RNA_INFO:
        return rna_info(arguments, world)

    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        return _argument_error(name, "`code` is required and must be a non-empty string.")
    purpose = arguments.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return _argument_error(
            name,
            "`purpose` is required: one imperative line naming what the call does and to "
            "which object, e.g. \"Scale Cube 1.3x on Z\".",
        )
    purpose = purpose.strip()
    if len(purpose) > PURPOSE_MAX:
        return _argument_error(
            name, f"`purpose` is {len(purpose)} characters; the maximum is {PURPOSE_MAX}."
        )

    return run_python(code, purpose, prelude(world), limits=limits)
