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

# Where each value came from, for the one label the preferences page and the
# probes print. Never the value itself - see `fingerprint`.
FROM_PREFS = "prefs"
FROM_ENV = "env"
FROM_DEFAULT = "default"
FROM_NONE = "not set"

# The provider's *current* small-model name, measured live in ticket 16 - the
# response echoed `deepseek-flash` back. Deliberately not the legacy
# `deepseek-v4-flash`, which that same run showed is accepted while silently
# serving a retired model: that trap is why this default is a *measured* string,
# and why `DEEPSEEK_MODEL` overrides it rather than the reverse.
DEFAULT_MODEL = "deepseek-flash"

# The endpoint's own default, and the **last** layer only: a preference or
# `DEEPSEEK_API_URL` beats it, and it is not applied at all on the env-only path
# (`config()` with no preferences), so no existing probe can be pointed at a real
# host by a missing variable. Ticket 16 read this string from
# `api-docs.deepseek.com` as the provider's documented default rather than
# recalling it, and it is the value this repo's own `.env` uses - which is the
# point, because the alternative is a human launching Blender from the Finder and
# being asked to type a base URL before anything works. `AGENTS.md` says never
# hard-code this; that rail is about *probes guessing an endpoint*, and this is
# the documented one, one layer below both overrides.
DEFAULT_BASE_URL = "https://api.deepseek.com"

# ---------------------------------------------------------------------------
# The per-request output ceiling.
#
# Ticket 16 measured the two things this has to respect: thinking is on by
# default and is billed as completion tokens (25 of 64 in one probe, 64 of 64 in
# another), and it shares the output budget with the visible reply *and* with the
# code inside a tool call. So a small cap does not shorten a reply - it deletes a
# turn. That was not hypothetical: `tools/live_turn_probe.py`'s first live run,
# 2026-09-26, asked for a football and came back `finish_reason=length` in 10.1s
# with an empty transcript and no tool call at all. The entire 2,048-token cap
# went on reasoning the code never saw.
#
# The fix follows how VS Code's own Copilot extension sets a request's limit:
# the ceiling is a property of the **model**, resolved per request, and the
# request asks for that model's maximum rather than for a number picked by hand.
#
#   * `extensions/copilot/src/extension/prompt/node/chatMLFetcher.ts` -
#     `const maxResponseTokens = chatEndpoint.maxOutputTokens;` then
#     `requestOptions = { max_tokens: maxResponseTokens, ...requestOptions }`,
#     i.e. every request defaults to the selected model's own output limit.
#   * `extensions/copilot/src/extension/byok/vscode-node/openRouterProvider.ts` -
#     the model's declared `max_completion_tokens`, falling back to a
#     `DEFAULT_MAX_OUTPUT_TOKENS` constant when the model declares none.
#
# The numbers below are the DeepSeek family's documented maximum output (ticket
# 16's table, read from `api-docs.deepseek.com`: 1M-token context, 384K max
# output), and 384,000 is **measured accepted**, not assumed: the third live run
# sent it to `deepseek-flash` and completed the whole turn in 37.6s.
#
# That provider also clamps to half the context window, which is a no-op here -
# 384K of a 1M window - so this does not repeat the clamp, because doing so would
# mean copying the window constant into the one module that deliberately knows
# nothing about budgets (`context.py` owns it, and reads this module the other
# way round). A ceiling that ever exceeded half a window would be a table entry to
# fix, not a formula to add.
MAX_OUTPUT_TOKENS = {
    "deepseek-flash": 384_000,
    "deepseek-v4-pro": 384_000,
}
DEFAULT_MAX_OUTPUT_TOKENS = 384_000


def max_output_tokens(model: str) -> int:
    """The ceiling to ask for, for *this* model.

    An unrecognised model string - a name from next quarter, or a local gateway's
    alias - gets the family's documented maximum rather than a small conservative
    number. Both directions have a failure mode: over-asking makes a provider that
    disagrees say so with a 400 naming the parameter, which is loud, cheap and
    fixable on the spot; under-asking silently spends a whole request on reasoning
    and ends the turn, which is exactly what this replaced. The reference makes the
    *user* declare the number for a custom endpoint; here the model field carries
    the name and this table carries the ceilings.
    """
    return MAX_OUTPUT_TOKENS.get((model or "").strip(), DEFAULT_MAX_OUTPUT_TOKENS)


class Config:
    """Everything needed to make a request, and whether it is all there."""

    __slots__ = (
        "base_url",
        "api_key",
        "model",
        "problem",
        "key_source",
        "url_source",
        "model_source",
        "max_tokens",
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        problem: str,
        key_source: str = FROM_NONE,
        url_source: str = FROM_NONE,
        model_source: str = FROM_NONE,
        max_tokens: int | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.problem = problem
        self.key_source = key_source
        self.url_source = url_source
        self.model_source = model_source
        # Resolved here from the model, so a `Config` built by hand - which is
        # what `tools/loop_wire_probe.py` does for its scripted provider - still
        # carries a sane ceiling instead of zero.
        self.max_tokens = int(max_tokens) if max_tokens else max_output_tokens(model)

    def describe(self) -> str:
        """Which route each value came from, for a human to read.

        The key appears as its last four characters at most (`fingerprint`), the
        way ticket 05 §2 asks: enough to tell two keys apart while rotating, not
        enough to be a leak. The URL and the model are not secret.
        """
        return (
            f"key: {self.key_source} {fingerprint(self.api_key)}"
            f" | url: {self.url_source} {self.base_url or 'not set'}"
            f" | model: {self.model_source} {self.model or 'not set'}"
        )

    def __repr__(self) -> str:  # never let a stray repr print the key
        return (
            f"Config(base_url={self.base_url!r}, api_key=<{len(self.api_key)} chars>,"
            f" model={self.model!r}, problem={self.problem!r})"
        )


def fingerprint(secret: str) -> str:
    """`…A1B2` - the last four characters, or `not set` when there is nothing.

    The whole point is that it cannot reconstruct the value, so it is safe on a
    label and in a probe log. A key shorter than four characters fingerprints to
    the bullets rather than to itself.
    """
    secret = (secret or "").strip()
    if not secret:
        return "not set"
    if len(secret) <= 4:
        return "\u2026????"
    return "\u2026" + secret[-4:]


def _pref(prefs, name: str) -> str:
    """One preference as a stripped string, or `""`.

    Never raises: `prefs` is `None` for every probe that calls `config()` with no
    argument, a stub `SimpleNamespace` in `tools/panel_draw_smoke.py`, and a real
    `AddonPreferences` in Blender. A missing field means "not set here", which is
    exactly what makes the next layer apply.
    """
    try:
        return str(getattr(prefs, name, "") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable preference is an empty one
        return ""


def _not_configured(missing: list[tuple[str, str]]) -> str:
    """The sentence that answers "why did Send do nothing?".

    Both routes are named, always. Ticket 05 §3 rejected `self.report` as the
    surface precisely because the interesting failure here is a user who cannot
    tell whether they are missing a setting or looking at the wrong screen: a bare
    "not configured" is the least interesting bug in the project. So: the field to
    fill in, and the shell incantation, in one string - and the variable's name,
    because a user who set it in `.zshrc` needs to be told that a Dock launch does
    not see it (ticket 05: launchd does not inherit the shell's environment).
    """
    if not missing:
        return ""
    parts = " and ".join(f"{what} ({name})" for what, name in missing)
    return (
        f"{parts} not set. Fill in the Copilot settings under Edit \u25b8 Preferences "
        "\u25b8 Add-ons \u25b8 Copilot, or launch Blender from a shell that has the "
        "repo's .env loaded:  set -a; . ./.env; set +a"
    )


def config(prefs=None) -> Config:
    """Resolve the endpoint, layering **preference -> environment -> default**.

    `prefs` is the add-on's `BlenderCopilotPreferences`, and passing it is what
    makes a Blender launched from the Finder able to send at all - the credential
    has no other way in, because Blender's Python has no keychain (ticket 05, and
    the value is plaintext in `userpref.blend`; the field's own copy says so).

    Called **with no argument** it is env-only, exactly as it shipped, and that is
    load-bearing rather than tidy: every probe that predates the preferences calls
    it that way, and a default endpoint in that path would send a probe with a
    faked key to a real host. The base URL's default applies only when a
    preferences object is supplied, which is the in-Blender route a human uses.

    A `.env` is still **not** read automatically - by Blender, or by this addon -
    so the launching shell has to have loaded it. The problem string says so.
    """
    pref_url = _pref(prefs, "base_url")
    pref_key = _pref(prefs, "api_key")
    pref_model = _pref(prefs, "model")
    env_url = (os.environ.get(URL_ENV) or "").strip()
    env_key = (os.environ.get(KEY_ENV) or "").strip()
    env_model = (os.environ.get(MODEL_ENV) or "").strip()

    if pref_url:
        base_url, url_source = pref_url, FROM_PREFS
    elif env_url:
        base_url, url_source = env_url, FROM_ENV
    elif prefs is not None:
        base_url, url_source = DEFAULT_BASE_URL, FROM_DEFAULT
    else:
        base_url, url_source = "", FROM_NONE
    # A trailing slash would produce `https://host//chat/completions`, which some
    # gateways 404 rather than normalise.
    base_url = base_url.rstrip("/")

    if pref_key:
        api_key, key_source = pref_key, FROM_PREFS
    elif env_key:
        api_key, key_source = env_key, FROM_ENV
    else:
        api_key, key_source = "", FROM_NONE

    if pref_model:
        model, model_source = pref_model, FROM_PREFS
    elif env_model:
        model, model_source = env_model, FROM_ENV
    else:
        model, model_source = DEFAULT_MODEL, FROM_DEFAULT

    missing = []
    if not api_key:
        missing.append(("The API key", KEY_ENV))
    if not base_url:
        missing.append(("The base URL", URL_ENV))
    return Config(
        base_url,
        api_key,
        model,
        _not_configured(missing),
        key_source,
        url_source,
        model_source,
    )


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
            # Per request, from the model's own ceiling (`config` resolved it):
            # every round of a turn asks for the maximum the model offers, because
            # thinking and the tool call's code both come out of it and a cap that
            # runs out mid-thought ends the turn with nothing to show.
            "max_tokens": cfg.max_tokens,
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
