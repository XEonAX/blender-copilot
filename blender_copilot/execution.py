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

`execute_tool` returns the shape the loop consumes, not a bare envelope:

    {"ok": bool, "envelope": {...}, "content": str, "detail": str, "summary": str}

`content` is the wire `tool` message (compact JSON, shared-capped); `detail` is the
same envelope pretty-printed for the panel's expander. The loop treats them as
opaque strings, which is exactly why `conversation.py` can stay free of this
module.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback
import types

# The name Blender's own `code.interact` machinery uses for model code, so a
# traceback frame can be identified by filename.
FILENAME = "<model>"

RUN_PYTHON = "run_blender_python"

# Ticket 06's caps. stdout keeps head *and* tail (setup prints at the head,
# errors at the tail); stderr and the traceback keep the tail, which is where the
# cause is.
STDOUT_CAP = 4000
STDERR_CAP = 2000
TRACEBACK_CAP = 2000
RESULT_CAP = 8000      # the shared cap, applied to the serialised envelope
RAW_ARGS_CAP = 500     # how much of a malformed arguments string is fed back
PURPOSE_MAX = 80

# The three tools' registry, as OpenAI tool schemas. Only one exists in this
# pass; `get_scene_info` and `get_rna_info` are the next ticket, and they arrive
# here as two more entries.
RUN_BLENDER_PYTHON_SCHEMA = {
    "type": "function",
    "function": {
        "name": RUN_PYTHON,
        "description": (
            "Run Python inside the live Blender session and return its output as JSON. "
            "The code runs on Blender's main thread with the full bpy API and NO sandbox: "
            "it can read and change the user's scene, write files and reach the network. "
            "It cannot be cancelled and has no time limit, so a `while True:` loop freezes "
            "Blender - keep every call short and do one meaningful thing. `bpy`, `C` "
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

SCHEMAS = [RUN_BLENDER_PYTHON_SCHEMA]


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


def run_python(code: str, purpose: str, bindings: dict) -> dict:
    """Exec `code` with a fresh namespace and return the envelope.

    Never raises on the model's behalf: anything the code throws - including
    `SystemExit` and `KeyboardInterrupt` - is caught as `BaseException` and
    reported, because a raised exception here would take the loop with it.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    truncated = False
    note = None

    module = types.ModuleType("__main__")
    module.__dict__.update(bindings)
    previous_main = sys.modules.get("__main__")
    previous_stdin = sys.stdin
    error = None
    try:
        # `compile` separately, so a SyntaxError carries lineno/offset.
        compiled = compile(code, FILENAME, "exec")
        sys.modules["__main__"] = module
        sys.stdin = _EofStdin()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compiled, module.__dict__)
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
            "note": None,
            "error": None,
        }
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
    return _result(envelope)


def _result(envelope: dict) -> dict:
    """The envelope, plus the two renderings the loop passes around opaquely."""
    content = json.dumps(envelope, ensure_ascii=False, default=str)
    if len(content) > RESULT_CAP:
        # The shared cap, as a last resort: the per-field caps above normally keep
        # this from firing at all. The elided string is no longer valid JSON, and
        # that is the honest trade - an unreadable tail beats a blown context
        # window, and the flag says so.
        elided, _ = elide(content, RESULT_CAP)
        envelope = dict(envelope)
        envelope["truncated"] = True
        envelope["note"] = f"result elided to {RESULT_CAP} chars"
        content = elided
    return {
        "ok": bool(envelope.get("ok")),
        "envelope": envelope,
        "content": content,
        "detail": json.dumps(envelope, ensure_ascii=False, indent=2, default=str),
        "summary": envelope.get("summary") or "",
    }


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


def execute_tool(call: dict, bindings) -> dict:
    """Run one wire tool call. Never raises; always returns the loop's shape.

    `bindings` is a callable returning the prelude dict, not the dict itself, so
    that nothing touches `bpy` unless a call is actually dispatched.
    """
    function = call.get("function") or {}
    name = function.get("name") or ""
    if name != RUN_PYTHON:
        # Ticket 09 §6: an unknown or empty tool name is a `tool_argument_error`
        # naming it, and nothing is executed. The registry is one entry long in
        # this pass, so "unknown" is the whole world outside it.
        return _argument_error(
            name or "(none)",
            f"Unknown tool {name!r}. The only tool available is {RUN_PYTHON}.",
        )

    raw = function.get("arguments")
    arguments = parse_arguments(call)
    if arguments is None:
        return _argument_error(
            name,
            "The arguments were not a JSON object. Send them as JSON with `code` and `purpose`.",
            raw=raw if isinstance(raw, str) else json.dumps(raw, default=str),
        )

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

    return run_python(code, purpose, bindings() if callable(bindings) else bindings)
