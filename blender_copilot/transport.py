"""The worker subprocess, from the parent's side.

Ticket 11 decided the transport: Blender's own bundled `python3.13`, launched as
`sys.executable <pkg>/_worker.py`, speaking newline-delimited JSON over
stdin/stdout, with `proc.kill()` as the terminal Stop path. This module is that
decision built rather than designed.

It imports no `bpy` and nothing else from this package, on purpose:

  * the worker's half of the protocol must be exercisable without a GUI, and
    `bpy.app.timers` never pump under `blender -b` - so `tools/transport_smoke.py`
    drives this module from plain CPython under Blender's bundled interpreter;
  * nothing here may touch the scene, so there is no reason for it to see `bpy`.

Three rules from ticket 11 are load-bearing, and all three are rules about
*not* blocking Blender's main thread:

  * the main thread never waits for the child, not even for `ready`;
  * the main thread never joins a thread and never blocks on a read;
  * the main thread never calls `wait()`.

So: two daemon reader threads (stdout into a `queue.Queue`, stderr into a
bounded ring), and a `tick()` the drain timer calls which only ever does
non-blocking work. Launch-to-ready measured 0.021 s and a JSON round trip
0.02 ms, so the IPC tax is two orders of magnitude below one timer tick.

`ready` never reaches the conversation: whether the child is up is transport
state. Everything after it is an event the transcript may care about.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time

# The worker ships in the package, next to this file. Ticket 11's launch flavor:
# a bare script, not `multiprocessing` spawn - our code cannot be imported by
# module name in the child (`bl_ext` is a package Blender fakes at runtime in
# the parent only), so spawn would need a package import that runs `import bpy`.
WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_worker.py")

READY_TIMEOUT = 5.0   # ticket 11: a missing `ready` within ~5 s is fatal
CANCEL_GRACE = 2.0    # ticket 11: `proc.kill()` after this much silence
DRAIN_LIMIT = 400     # events per tick, so a burst cannot stall a repaint
STDERR_TAIL = 200     # stderr lines kept for a crash report

URL_ENV = "DEEPSEEK_API_URL"
KEY_ENV = "DEEPSEEK_API_KEY"
MODEL_ENV = "DEEPSEEK_MODEL"

# The provider's *current* small-model name, measured live in ticket 16 - the
# response echoed `deepseek-flash` back. Deliberately not the legacy
# `deepseek-v4-flash`, which that same run showed is accepted while silently
# serving a retired model: that trap is why this default is a *measured* string,
# and why `DEEPSEEK_MODEL` overrides it rather than the reverse.
DEFAULT_MODEL = "deepseek-flash"

# Thinking is on by default and billed as completion tokens (ticket 16: 64 of 64
# in one probe), so a small cap can be spent entirely on reasoning and return an
# empty visible reply. 2,048 tokens is roughly $0.0012 of output at Flash rates.
MAX_TOKENS = 2048


class Config:
    """Everything needed to make a request, and whether it is all there."""

    __slots__ = ("base_url", "api_key", "model", "problem")

    def __init__(self, base_url: str, api_key: str, model: str, problem: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.problem = problem

    def __repr__(self) -> str:  # never let a stray repr print the key
        return (
            f"Config(base_url={self.base_url!r}, api_key=<{len(self.api_key)} chars>,"
            f" model={self.model!r}, problem={self.problem!r})"
        )


def config() -> Config:
    """Read the endpoint from the environment.

    A `.env` is **not** read automatically - by Blender, or by this addon - so
    the launching shell has to have loaded it (`set -a; . ./.env; set +a`). The
    message below says exactly that, because "why does Send do nothing" is the
    least interesting bug in the project.
    """
    base_url = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
    api_key = (os.environ.get(KEY_ENV) or "").strip()
    model = (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

    missing = []
    if not base_url:
        missing.append(URL_ENV)
    if not api_key:
        missing.append(KEY_ENV)
    problem = ""
    if missing:
        problem = (
            f"{' and '.join(missing)} not set. Launch Blender from a shell that has "
            "the repo's .env loaded:  set -a; . ./.env; set +a"
        )
    return Config(base_url, api_key, model, problem)


class Worker:
    """One child at a time: started lazily, restarted only on the next send."""

    def __init__(self) -> None:
        self._proc = None
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._stderr: "collections.deque[str]" = collections.deque(maxlen=STDERR_TAIL)
        self._lock = threading.Lock()
        self._ready = False
        self._started_at = 0.0
        self._turn = 0
        self._inflight = False
        self._deliberate_kill = False
        self._cancel_deadline: float | None = None

    # -- state ---------------------------------------------------------------
    @property
    def alive(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def busy(self) -> bool:
        """True while events for a turn may still arrive, so the drain timer
        knows to stay registered even after the UI has stopped streaming."""
        return self._inflight or self._cancel_deadline is not None

    def stderr_tail(self, limit: int = 2000) -> str:
        return "\n".join(self._stderr)[-limit:]

    def _pid(self) -> int | None:
        with self._lock:
            proc = self._proc
        return proc.pid if proc is not None else None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> str | None:
        """Spawn the child if there is not one. Returns None, or a reason it
        could not start - the caller shows that verbatim."""
        if self.alive:
            return None
        try:
            proc = subprocess.Popen(
                [sys.executable, WORKER_PATH],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the panel
            return f"Could not start the worker: {type(exc).__name__}: {exc}"
        with self._lock:
            self._proc = proc
        self._ready = False
        self._started_at = time.monotonic()
        self._deliberate_kill = False
        self._cancel_deadline = None
        self._flush()
        # Outside the lock, and daemon so Blender's shutdown never waits on them.
        threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True).start()
        return None

    def _pump_stdout(self, proc) -> None:
        """The only blocking read in the addon, and it is not the main thread."""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {"ev": "protocol_error", "message": line[:400]}
                event.setdefault("pid", proc.pid)
                self._events.put(event)
        except Exception as exc:  # noqa: BLE001 - the child died mid-line
            self._events.put({"ev": "protocol_error", "pid": proc.pid,
                              "message": f"{type(exc).__name__}: {exc}"})
        finally:
            code = proc.wait()
            self._events.put({"ev": "exit", "pid": proc.pid, "code": code,
                              "stderr": self.stderr_tail()})

    def _pump_stderr(self, proc) -> None:
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip())
        except Exception:
            pass

    def kill(self) -> None:
        """The terminal Stop path (ticket 11). Never the first resort."""
        with self._lock:
            proc = self._proc
        self._deliberate_kill = True
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def shutdown(self, timeout: float = 1.0) -> None:
        """Polite, then not. Called from `unregister`, where a brief block is
        acceptable and an orphaned child is not."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        self._write({"cmd": "shutdown"})
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            self.kill()
        with self._lock:
            self._proc = None
        self._ready = False
        self._inflight = False
        self._flush()

    def reset(self) -> None:
        """The Retry button: forget everything and let the next send respawn."""
        self.shutdown(timeout=0.2)
        self._cancel_deadline = None
        self._deliberate_kill = False
        self._stderr.clear()
        self._flush()

    def _flush(self) -> None:
        """Drop queued events. Only safe between turns - and the only thing that
        makes a stale `exit` from a previous child unable to kill the next one."""
        try:
            while True:
                self._events.get_nowait()
        except queue.Empty:
            pass

    # -- the protocol --------------------------------------------------------
    def _write(self, command: dict) -> str | None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return "The worker is not running."
        try:
            proc.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            return f"The worker pipe closed: {type(exc).__name__}"
        return None

    def send(self, cfg: Config, messages: list, tools: list | None = None) -> str | None:
        """Start a turn. Never blocks; `ready` is not awaited (ticket 11).

        `tools` is the OpenAI tool array. It is passed in rather than imported so
        this module keeps knowing nothing about what the tools *are* - and it is
        sent every round, not just the first, because a round without the
        declaration is a round the model cannot call anything from.
        """
        reason = self.start()
        if reason:
            return reason
        self._turn += 1
        self._inflight = True
        self._cancel_deadline = None
        self._deliberate_kill = False
        command = {
            "cmd": "send",
            "id": self._turn,
            "base_url": cfg.base_url,
            "api_key": cfg.api_key,
            "model": cfg.model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
        }
        if tools:
            command["tools"] = tools
        return self._write(command)

    def cancel(self) -> None:
        """Ask, then - if the child does not answer within the grace - kill."""
        self._deliberate_kill = True
        self._cancel_deadline = time.monotonic() + CANCEL_GRACE
        self._write({"cmd": "cancel", "id": self._turn})

    # -- the drain -----------------------------------------------------------
    def tick(self) -> list[dict]:
        """Non-blocking. Drains what the threads queued, plus time-based verdicts.

        This is the whole of the main thread's involvement with the transport.
        """
        pid = self._pid()
        events: list[dict] = []

        for _ in range(DRAIN_LIMIT):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break

            # A stale event from a previous child must not touch this turn.
            if event.get("pid") is not None and pid is not None and event["pid"] != pid:
                continue

            kind = event.get("ev")
            if kind == "ready":
                self._ready = True
                continue
            if kind == "exit":
                was_ready = self._ready
                deliberate = self._deliberate_kill or self._cancel_deadline is not None
                with self._lock:
                    self._proc = None
                self._ready = False
                self._inflight = False
                self._cancel_deadline = None
                self._deliberate_kill = False
                if deliberate:
                    events.append({"ev": "stopped"})
                elif not was_ready:
                    events.append({
                        "ev": "startup_failed",
                        "kind": "worker_unavailable",
                        "fatal": True,
                        "code": event.get("code"),
                        "stderr": (event.get("stderr") or "")[-2000:],
                        "message": (
                            f"The worker exited before it was ready "
                            f"(code {event.get('code')}). {self._hint()}"
                        ),
                    })
                else:
                    events.append(event)
                continue
            if kind in ("done", "error", "stopped"):
                self._inflight = False
                self._cancel_deadline = None
                self._deliberate_kill = False
            events.append(event)

        if self._inflight and self._cancel_deadline is not None:
            if time.monotonic() > self._cancel_deadline:
                # Ticket 11's terminal path: the child ignored the cancel, so
                # the turn ends here whether or not it would have answered.
                self._inflight = False
                self._cancel_deadline = None
                self.kill()
                events.append({"ev": "stopped"})

        if self.alive and not self._ready and time.monotonic() - self._started_at > READY_TIMEOUT:
            self._deliberate_kill = True   # suppress the crash report for our own kill
            self._inflight = False
            self._cancel_deadline = None
            self.kill()
            events.append({
                "ev": "startup_failed",
                "kind": "worker_unavailable",
                "fatal": True,
                "message": f"The worker started but never reported ready. {self._hint()}",
                "stderr": self.stderr_tail(),
            })
        return events

    def _hint(self) -> str:
        tail = self.stderr_tail(400)
        if tail:
            return f"Last stderr: {tail}"
        return "It exited without a word; check that the bundled python can import requests."


worker = Worker()
