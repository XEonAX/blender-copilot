#!/usr/bin/env python3
"""The transport child: HTTP and SSE, and nothing else.

Ticket 11 decided this shape and these rules, and they are load-bearing:

  * **standalone.** It imports no `bpy` and nothing from the extension package,
    so `import bpy` cannot accidentally work in here. That isolation is the
    whole point of paying for a process boundary.
  * **stdout is the protocol.** Anything diagnostic goes to stderr; one stray
    `print()` would corrupt the stream.
  * **the API key is never printed, logged or echoed.** It arrives in the
    `send` command, goes into one `Authorization` header, and is not stored.

Protocol, newline-delimited JSON over stdin/stdout.

    parent -> child
      {"cmd":"send","id":N,"base_url":...,"api_key":...,"model":...,
       "messages":[...],"max_tokens":N}
      {"cmd":"cancel","id":N}
      {"cmd":"shutdown"}

    child -> parent
      {"ev":"ready","pid":N}
      {"ev":"delta","id":N,"text":"..."}       visible reply text
      {"ev":"reasoning","id":N,"chars":N}      thinking: counted, never shown
      {"ev":"done","id":N,"finish_reason":...}  the reply ended; also carries
                                                 {"tool_calls":[...]} when the
                                                 model asked for tools
      {"ev":"stopped","id":N}
      {"ev":"error","id":N,"kind":...,"status":N,"message":...,"detail":...}
      {"ev":"protocol_error","message":...}

`id` is echoed so the parent can ignore an event from a turn it has abandoned.

`tool_calls` ride on `done` rather than on their own event, deliberately. They are
one atomic fact - the reply finished, and here is what it asked for - and a
separate event could arrive after the parent had already decided the turn was
over. The parent then has two things to do with them entirely on its own side:
parse `function.arguments` (a JSON **string**, measured in ticket 16, not an
object), and answer every `tool_call.id` with a `tool` message or the provider
refuses the next request (HTTP 400, same measurement).

Two threads, on purpose: the main thread owns the request and blocks inside
`iter_lines()`, and a reader thread owns stdin so a `cancel` is *delivered*
while that happens. A single-threaded loop could not hear Stop mid-stream, which
is the one moment it matters.

Run it by hand to watch the protocol:

    echo '{"cmd":"send","id":1,"base_url":"https://api.deepseek.com",
           "api_key":"...","model":"deepseek-flash",
           "messages":[{"role":"user","content":"hi"}]}' | python3 _worker.py
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading

import requests

# The connect timeout is for reaching the host; the read timeout is the longest
# gap allowed *between* chunks. Ticket 11 suggested `(5, 1.0)` to cap a Stop at
# ~1 s, but that was written before ticket 16 measured that thinking tokens
# stream too: a one-second gap is normal on a large prefill, and aborting there
# would look like a network fault when nothing is wrong. Cancellation does not
# depend on it either way - `raw.shutdown()` unblocks the reader instantly
# (measured in ticket 03), and `proc.kill()` is the backstop in the parent.
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 30.0


def emit(event: dict) -> None:
    """One protocol line. Never raises: a broken pipe means the parent is gone."""
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class ServerError(Exception):
    """An `{"error": ...}` object arriving mid-stream, on an already-200 response."""


class Session:
    """One in-flight request. `cancel()` is called from the stdin thread."""

    def __init__(self, command: dict) -> None:
        self.id = command.get("id")
        self.base_url = (command.get("base_url") or "").rstrip("/")
        self.api_key = command.get("api_key") or ""
        self.model = command.get("model") or ""
        self.messages = command.get("messages") or []
        self.tools = command.get("tools") or []
        self.max_tokens = int(command.get("max_tokens") or 512)
        self.cancelled = threading.Event()
        self._response = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            response = self._response
        if response is None:
            return
        # Measured in ticket 03: `response.close()` does NOT unblock a blocked
        # reader (30 s vs 1.5 s); shutting the raw socket down does. Guarded
        # because it is urllib3 behaviour that `requests` does not document,
        # and the bounded read timeout is the mechanism underneath it.
        try:
            response.raw.shutdown()
        except Exception:
            pass

    # -- the request ---------------------------------------------------------
    def run(self) -> None:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
            "max_tokens": self.max_tokens,
        }
        if self.tools:
            payload["tools"] = self.tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            response = requests.post(
                url, headers=headers, json=payload, stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except Exception as exc:  # noqa: BLE001 - classified and reported
            emit(self._exception_error("request", exc))
            return

        with self._lock:
            self._response = response
        try:
            if self.cancelled.is_set():
                emit({"ev": "stopped", "id": self.id})
                return
            if response.status_code != 200:
                emit(self._http_error(response))
                return
            finish_reason, tool_calls = self._consume(response)
            if self.cancelled.is_set():
                emit({"ev": "stopped", "id": self.id})
                return
            emit(
                {
                    "ev": "done",
                    "id": self.id,
                    "finish_reason": finish_reason,
                    "tool_calls": tool_calls,
                }
            )
        except ServerError as exc:
            emit({"ev": "error", "id": self.id, "kind": "server",
                  "message": str(exc), "detail": ""})
        except Exception as exc:  # noqa: BLE001 - classified and reported
            if self.cancelled.is_set():
                emit({"ev": "stopped", "id": self.id})
            else:
                emit(self._exception_error("stream", exc))
        finally:
            try:
                response.close()
            except Exception:
                pass
            with self._lock:
                self._response = None

    def _consume(self, response) -> tuple[str | None, list[dict]]:
        """Read the SSE stream. Returns the `finish_reason` and the tool calls.

        The accumulation rules are ticket 03 §2, with the shapes ticket 16
        measured: `content` concatenates, `[DONE]` ends. Tool calls arrive as
        **fragments** in `delta.tool_calls`, each carrying the `index` of its slot
        in the final array; `id`, `type` and `function.name` arrive once and the
        `function.arguments` fragments must be **concatenated**. What comes out is
        therefore a JSON *string*, which is what the provider sends and what the
        parent has to expect.
        """
        finish_reason = None
        slots: dict[int, dict] = {}
        for line in response.iter_lines(decode_unicode=True):
            if self.cancelled.is_set():
                break
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                # A keep-alive or a comment. Dropping it is correct: the stream
                # is line-oriented and a non-JSON data line carries no content.
                continue
            error = event.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise ServerError(message or "the server reported an error mid-stream")
            choice = (event.get("choices") or [{}])[0] or {}
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                emit({"ev": "delta", "id": self.id, "text": text})
            reasoning = delta.get("reasoning_content")
            if reasoning:
                # Thinking is on by default and billed as completion tokens
                # (ticket 16). Its *length* is what the panel needs - it is what
                # makes "waiting" honest during a long think - so only the count
                # crosses the pipe, never the text.
                emit({"ev": "reasoning", "id": self.id, "chars": len(reasoning)})
            for fragment in delta.get("tool_calls") or []:
                self._accumulate(slots, fragment)
        return finish_reason, [slots[index] for index in sorted(slots)]

    @staticmethod
    def _accumulate(slots: dict, fragment: dict) -> None:
        """Fold one `delta.tool_calls` fragment into its slot.

        `index` is the slot in the final array, and the first fragment for a slot
        is usually the only one carrying `id` and `name`. Everything is `.get`-
        guarded because a provider is free to split the fields across chunks
        differently, and a KeyError here would take down the whole turn.
        """
        index = fragment.get("index")
        if not isinstance(index, int):
            index = 0
        slot = slots.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        if fragment.get("type"):
            slot["type"] = fragment["type"]
        function = fragment.get("function") or {}
        if function.get("name"):
            slot["function"]["name"] = function["name"]
        arguments = function.get("arguments")
        if arguments:
            slot["function"]["arguments"] += arguments

    # -- failures ------------------------------------------------------------
    def _http_error(self, response) -> dict:
        """Classify a non-200 the way ticket 09 §6's table asks."""
        status = response.status_code
        code = ""
        message = ""
        try:
            body = response.json()
            error = body.get("error") or {}
            code = error.get("code") or ""
            message = error.get("message") or ""
            detail = json.dumps(body, ensure_ascii=False)[:600]
        except Exception:
            detail = (getattr(response, "text", "") or "")[:600]

        if status in (401, 403):
            kind = "auth"
        elif status == 429:
            kind = "rate_limit"
        elif code in ("model_not_found", "invalid_model") or (
            status in (400, 404) and "model" in message.lower()
        ):
            kind = "model_not_found"
        else:
            kind = "http_error"

        event = {
            "ev": "error",
            "id": self.id,
            "kind": kind,
            "status": status,
            "message": message or f"The provider returned HTTP {status}.",
            "detail": detail,
        }
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            event["retry_after"] = retry_after
        return event

    def _exception_error(self, phase: str, exc: Exception) -> dict:
        if isinstance(exc, requests.exceptions.Timeout):
            kind = "timeout"
        elif isinstance(exc, requests.exceptions.SSLError):
            kind = "tls"
        elif isinstance(exc, requests.exceptions.ConnectionError):
            kind = "network"
        else:
            kind = "internal"
        return {
            "ev": "error",
            "id": self.id,
            "kind": kind,
            "message": f"{type(exc).__name__}: {exc}",
            "detail": json.dumps({"phase": phase}),
        }


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    work: "queue.Queue[dict | None]" = queue.Queue()
    state: dict = {"session": None}
    lock = threading.Lock()
    cancelled_ids: set = set()

    def reader() -> None:
        """stdin, forever. Stays responsive while the main thread streams."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except ValueError:
                emit({"ev": "protocol_error", "message": "line was not JSON"})
                continue
            name = command.get("cmd")
            if name == "send":
                work.put(command)
            elif name == "cancel":
                # Remembered as well as delivered: a cancel that overtakes the
                # send it refers to must still count, or Stop is silently lost.
                cancelled_ids.add(command.get("id"))
                with lock:
                    session = state["session"]
                if session is not None:
                    session.cancel()
            elif name == "shutdown":
                cancelled_ids.add(command.get("id"))
                with lock:
                    session = state["session"]
                if session is not None:
                    session.cancel()
                work.put(None)
                return
        work.put(None)  # EOF: the parent is gone, so exit

    emit({"ev": "ready", "pid": os.getpid()})
    threading.Thread(target=reader, daemon=True).start()

    while True:
        command = work.get()
        if command is None:
            break
        session = Session(command)
        if session.id in cancelled_ids:
            session.cancelled.set()
        with lock:
            state["session"] = session
        session.run()
        with lock:
            state["session"] = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
