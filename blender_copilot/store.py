"""Where a conversation lives between Blender sessions. No `bpy` in this file.

*Where chat history lives* settled the shape, and this module is that shape built
rather than re-argued:

  * **JSON under the extension's user directory**, one file per conversation plus
    a small `active.json` index. Never inside a `.blend`: a `Text` datablock was
    *measured* to persist into the file and travel with it, which is exactly the
    disqualifier for something a user mails to a colleague or attaches to a bug
    report.
  * **Scoped per `.blend` path**, with the *old* path kept as an alias after Save
    As, so both names find the same conversation rather than forking the thread.
  * **One rolling conversation per scope**, messages stored verbatim in the
    provider's wire format - so restoring needs no translation and there is no
    lossy second schema. A display-only line (a transport error, a stop marker) is
    a transient of a live session and is deliberately not stored.
  * **Capped at 200 messages or 1 MiB, pruning whole turns from the front.** Whole
    turns, never single messages: a `tool` result whose `tool_calls` parent was
    dropped is rejected outright by the provider (HTTP 400, measured in ticket 16).

And an unsaved file - `bpy.data.filepath == ""` - is session-only, which the label
says out loud rather than pretending the conversation is somewhere.

What is *not* here is any `bpy`. The directory arrives as a string (or a callable
returning one, so `scope.py` can ask Blender lazily), and every rule above is
decidable without a scene - which is what lets `tests/test_store.py` run on plain
CPython, because a real extension directory needs Blender.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid

# The file format this module writes. 1 was `{id, scope, messages}` and is still
# read; 2 is that plus `meta` beside the wire `messages` - ticket 14 §5's additive
# amendment, which this module populates with `retention` and never treats as
# anything but information.
SCHEMA = 2

# The ratified caps (*Where chat history lives* §4).
CAP_MESSAGES = 200
CAP_BYTES = 1024 * 1024

CONVERSATIONS_DIR = "conversations"
INDEX_NAME = "active.json"
INDEX_VERSION = 1

SESSION_ONLY_LABEL = "unsaved \u2014 session only"
NO_STORE_LABEL = "no history folder \u2014 session only"

# One panel label's worth of a file name. Blender middle-clips a label that does
# not fit rather than wrapping it, and the header row shares the sidebar's width
# with the status, so a long basename is shortened at the front.
LABEL_CHARS = 28


def new_id() -> str:
    """A file-safe conversation id. Content-free on purpose: it names a file."""
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def scope_label(blend_path: str) -> str:
    """The header's short name for a scope: the file's basename, or that fact."""
    if not blend_path:
        return SESSION_ONLY_LABEL
    name = os.path.basename(blend_path.rstrip("/\\")) or blend_path
    if len(name) <= LABEL_CHARS:
        return name
    return "\u2026" + name[-(LABEL_CHARS - 1):]


# ---------------------------------------------------------------------------
# What a turn is
#
# The retention cap prunes turns, so "turn" is a storage concept and not only a
# display one. It is also the boundary ticket 05's receipt needs: the turn that
# just ended is the last span, and its tool results are what the receipt names.
# ---------------------------------------------------------------------------

def is_turn_start(message: dict) -> bool:
    return message.get("role") == "user"


def turn_spans(history: list[dict]) -> list[tuple[int, int]]:
    """Half-open `[start, end)` index ranges, one per turn, oldest first.

    A turn is a `user` message plus everything up to the next one - which is the
    shape the provider's own contract forces, since a `tool` result must follow
    the `tool_calls` that asked for it. Anything *before* the first `user` message
    (a hand-edited file, an interrupted prune) is its own span rather than being
    quietly absorbed into the first turn: it is the thing most likely to be
    broken, so pruning must be able to drop it first and a reader must be able to
    see it.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for index, message in enumerate(history):
        if is_turn_start(message):
            if index > start:
                spans.append((start, index))
            start = index
    if history:
        spans.append((start, len(history)))
    return spans


def turns(history: list[dict]) -> list[list[dict]]:
    """`turn_spans` as messages. Ticket 05's receipt reads the last one."""
    return [history[start:end] for start, end in turn_spans(history)]


def tool_call_ids(history: list[dict]) -> list[str]:
    """Every call id an assistant message asked about, in order."""
    ids: list[str] = []
    for message in history:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if call_id:
                ids.append(call_id)
    return ids


def tool_result_ids(history: list[dict]) -> list[str]:
    return [
        message.get("tool_call_id")
        for message in history
        if message.get("role") == "tool"
    ]


def unanswered(history: list[dict]) -> list[str]:
    """Calls with no `tool` result. The provider refuses such a history outright."""
    answered = set(tool_result_ids(history))
    return [call_id for call_id in tool_call_ids(history) if call_id not in answered]


def orphan_results(history: list[dict]) -> list[str]:
    """`tool` results whose call is not earlier in the same history."""
    orphaned: list[str] = []
    seen: list[str] = []
    for message in history:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in seen:
                orphaned.append(call_id)
        else:
            seen.extend(
                call.get("id") for call in (message.get("tool_calls") or [])
            )
    return orphaned


def encoded_size(history: list[dict]) -> int:
    """UTF-8 bytes of the wire JSON - the number the cap is really about.

    Not the size of the pretty-printed file on disk: the point of the cap is what
    the request would carry, and a conversation nobody can afford to send is not
    a conversation worth keeping. Indenting the file for legibility costs a few
    percent over this and is deliberate.
    """
    return len(json.dumps(history, ensure_ascii=False, default=str).encode("utf-8"))


def prune(
    history: list[dict],
    previous: dict | None = None,
    *,
    cap_messages: int = CAP_MESSAGES,
    cap_bytes: int = CAP_BYTES,
) -> tuple[list[dict], dict]:
    """Drop whole turns from the front until the history fits the caps.

    Returns `(kept, retention)`. Two rules, both from the ratified decision:

  * **Whole turns.** A cut is always at a turn boundary, so the kept history can
    still be sent: no `tool` result is ever separated from the call that produced
    it, and `unanswered()` stays empty.
  * **The newest turn is never dropped**, even when it alone exceeds the cap. A
    conversation truncated to nothing would be worse than an over-cap one, and the
    over-cap turn is the one the user just had.

    `previous` is the retention already recorded in the file, so a long
    conversation accumulates one count rather than restarting it on every save.
    `reason` names the constraint that was *violated* before pruning rather than
    the cut that happened to satisfy both, which is the honest reading of "why did
    this lose turns".
    """
    spans = turn_spans(history)
    if not history:
        return [], dict(previous or {})

    # Checked before any cut, not as the last iteration of the loop below: a
    # history that fits must come back untouched, and a loop that always cuts at
    # the next boundary would drop a turn for no reason at all.
    if len(history) <= cap_messages and encoded_size(history) <= cap_bytes:
        retention = dict(previous or {})
        retention.setdefault("turns_dropped", 0)
        retention.setdefault("dropped_through", None)
        retention.setdefault("reason", None)
        return list(history), retention

    keep_from = spans[-1][0] if len(spans) > 1 else 0
    if len(spans) > 1:
        for index in range(1, len(spans)):
            candidate = spans[index][0]
            kept = history[candidate:]
            if len(kept) <= cap_messages and encoded_size(kept) <= cap_bytes:
                keep_from = candidate
                break
            # No earlier cut fits either, so keep the newest turn and say so.
            keep_from = spans[-1][0]

    kept = list(history[keep_from:])
    dropped_turns = sum(1 for start, _ in spans if start < keep_from)
    if not dropped_turns:
        retention = dict(previous or {})
        retention.setdefault("turns_dropped", 0)
        retention.setdefault("dropped_through", None)
        retention.setdefault("reason", None)
        return kept, retention

    carried = int((previous or {}).get("turns_dropped") or 0)
    if len(history) > cap_messages:
        reason = f"cap_{cap_messages}_messages"
    else:
        reason = "cap_1MiB"
    return kept, {
        "turns_dropped": carried + dropped_turns,
        "dropped_through": now_iso(),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------

class Store:
    """One directory of conversations plus its index.

    `directory` is a string, or a callable returning one - the callable form is
    what lets `scope.py` ask Blender for the path lazily (and let the answer
    change if the user moves their config). An empty answer means there is nowhere
    to write, and everything below degrades to "keep it in memory and say so"
    rather than raising: `bpy.utils.extension_path_user` raises a `ValueError` for
    a package name that does not name an extension, which is *measured*, and it
    returns `""` when it cannot create the directory.
    """

    def __init__(
        self,
        directory="",
        *,
        cap_messages: int = CAP_MESSAGES,
        cap_bytes: int = CAP_BYTES,
    ) -> None:
        self._directory = directory
        self.cap_messages = cap_messages
        self.cap_bytes = cap_bytes

    # -- the directory -------------------------------------------------------
    def directory(self, create: bool = True) -> str:
        """The conversations directory, or `""` when there is not one."""
        root = self._directory() if callable(self._directory) else self._directory
        root = str(root or "")
        if not root:
            return ""
        if create:
            try:
                os.makedirs(root, exist_ok=True)
            except OSError:
                return ""
            if not os.path.isdir(root):
                return ""
        return root

    @property
    def available(self) -> bool:
        return bool(self.directory())

    # -- the index -----------------------------------------------------------
    def index(self) -> dict:
        """`active.json`, or `{}`. A corrupt index is a rebuild, not a failure."""
        directory = self.directory(create=False)
        if not directory:
            return {}
        try:
            with open(os.path.join(directory, INDEX_NAME), encoding="utf-8") as handle:
                index = json.load(handle)
        except (OSError, ValueError):
            return {}
        return index if isinstance(index, dict) else {}

    def _write_index(self, index: dict) -> bool:
        directory = self.directory()
        if not directory:
            return False
        return _write_atomic(os.path.join(directory, INDEX_NAME), json.dumps(index, indent=2))

    def ids_on_disk(self) -> list[str]:
        directory = self.directory(create=False)
        if not directory:
            return []
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        return sorted(
            name[:-5] for name in names
            if name.endswith(".json") and name != INDEX_NAME
        )

    def find(self, blend_path: str) -> str:
        """The id filed under `blend_path`, or `""`.

        The index is a convenience and the conversation headers are the truth, so
        a missing or corrupt `active.json` costs a scan rather than the history -
        and a hit found by scanning is written back, so it costs one scan, not one
        per lookup.
        """
        if not blend_path:
            return ""
        directory = self.directory()
        if not directory:
            return ""
        index = self.index()
        candidate = (index.get("active") or {}).get(blend_path) or ""
        if candidate and self.load(candidate) is not None:
            return candidate
        for conversation_id in self.ids_on_disk():
            record = self.load(conversation_id)
            if record is None:
                continue
            scope = record.get("scope") or {}
            names = [scope.get("blend_path") or "", *(scope.get("aliases") or [])]
            if blend_path in names:
                active = index.setdefault("active", {})
                active[blend_path] = conversation_id
                index["version"] = INDEX_VERSION
                self._write_index(index)
                return conversation_id
        return ""

    # -- one conversation ----------------------------------------------------
    def load(self, conversation_id: str) -> dict | None:
        directory = self.directory()
        if not directory or not conversation_id:
            return None
        try:
            with open(
                os.path.join(directory, f"{conversation_id}.json"), encoding="utf-8"
            ) as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
            return None
        return record

    def write(self, record: dict) -> bool:
        """Write one conversation, then point the index at it.

        Temp file plus `os.replace`, so a crash mid-write cannot leave a
        half-written conversation in place of a good one - the ratified decision
        asked for exactly this, and a torn JSON file is the failure it prevents.
        """
        directory = self.directory()
        conversation_id = str(record.get("id") or "")
        if not directory or not conversation_id:
            return False
        if not _write_atomic(
            os.path.join(directory, f"{conversation_id}.json"), json.dumps(record, indent=2)
        ):
            return False
        index = self.index()
        index["version"] = INDEX_VERSION
        active = index.setdefault("active", {})
        scope = record.get("scope") or {}
        for name in [scope.get("blend_path") or "", *(scope.get("aliases") or [])]:
            if name:
                active[name] = conversation_id
        self._write_index(index)
        return True

    def delete_all(self) -> int:
        """Remove the whole conversations directory. Returns how many files went.

        Guarded three ways, because this is the only call here that removes a
        tree and the path comes from Blender: it must actually be named
        `conversations`, it must not be a symlink (which `shutil.rmtree` refuses
        anyway, after the point where a wrong path would already have been
        followed), and it must exist. Anything else removes nothing and says so by
        returning 0.
        """
        directory = self.directory(create=False)
        if not directory or not os.path.isdir(directory):
            return 0
        if os.path.islink(directory):
            return 0
        if os.path.basename(os.path.normpath(directory)) != CONVERSATIONS_DIR:
            return 0
        try:
            removed = len([name for name in os.listdir(directory) if name.endswith(".json")])
        except OSError:
            return 0
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    def open(self, blend_path: str) -> tuple["Scope", list[dict]]:
        """The scope for a `.blend` path, and the history filed under it.

        A file with no conversation yet gets an empty scope and **no file**: an
        empty conversation is not worth writing, and creating one for every
        `.blend` a user ever opens would leave a directory full of nothing.
        """
        scope = Scope(self, blend_path)
        conversation_id = self.find(blend_path)
        if not conversation_id:
            return scope, []
        record = self.load(conversation_id)
        if record is None:
            return scope, []
        scope.id = conversation_id
        scope.meta = dict(record.get("meta") or {})
        scope.aliases = [
            name
            for name in ((record.get("scope") or {}).get("aliases") or [])
            if name and name != blend_path
        ]
        history = record.get("messages") or []
        return scope, [dict(message) for message in history]


class Scope:
    """Which conversation the session is showing, and where it is filed.

    Mutable and long-lived: `blend_path` changes on Save As, `id` appears on the
    first write, `meta` survives a round trip, and `aliases` accumulates the names
    a conversation used to have. `scope.py` owns the current one.
    """

    def __init__(self, store: Store, blend_path: str = "") -> None:
        self.store = store
        self.blend_path = blend_path
        self.id = ""
        self.aliases: list[str] = []
        self.meta: dict = {}

    @property
    def session_only(self) -> bool:
        """An unsaved file: the conversation lives for this session only."""
        return not self.blend_path

    @property
    def pinned(self) -> bool:
        """True once this conversation has a file of its own."""
        return bool(self.id) and not self.session_only

    @property
    def label(self) -> str:
        """What the panel header says the scope is. Short, and honest."""
        if self.session_only:
            return SESSION_ONLY_LABEL
        if not self.store.available:
            return NO_STORE_LABEL
        return scope_label(self.blend_path)

    def adopt(self, blend_path: str) -> bool:
        """Point this conversation at a new file path. True when that changed it.

        Save As, or the first save of an unsaved file. The conversation is not
        duplicated and not lost: it keeps its id and its file, the new path
        becomes the canonical one, and the old path joins the aliases so that
        opening it still finds *this* thread rather than starting an empty one.
        """
        if blend_path == self.blend_path:
            return False
        if self.blend_path and self.blend_path not in self.aliases:
            self.aliases.append(self.blend_path)
        self.blend_path = blend_path
        self.aliases = [name for name in self.aliases if name != blend_path]
        return True

    def save(self, history: list[dict]) -> bool:
        """Write the conversation under this scope. False when it cannot be.

        The retention cap is applied here, so the file is bounded and the
        in-memory conversation is not: what the *model* sees is ticket 14's
        projection, and truncating the user's own record mid-session on top of that
        would be two invisible forgettings instead of one visible one.
        """
        if self.session_only or not self.store.available:
            return False
        if not history and not self.id:
            # Nothing has been said here yet. A file is not created for every
            # `.blend` a user opens and never chats about - the conversation
            # appears on disk when there is a conversation to put there.
            return False
        kept, retention = prune(
            history,
            self.meta.get("retention"),
            cap_messages=self.store.cap_messages,
            cap_bytes=self.store.cap_bytes,
        )
        self.meta["retention"] = retention
        conversation_id = self.id or new_id()
        record = {
            "schema": SCHEMA,
            "id": conversation_id,
            "scope": {
                "blend_path": self.blend_path,
                "aliases": list(self.aliases),
            },
            "updated": now_iso(),
            "messages": kept,
            "meta": self.meta,
        }
        if not self.store.write(record):
            return False
        self.id = conversation_id
        return True


def _write_atomic(path: str, body: str) -> bool:
    """Write `path` through a temp file, so a crash cannot tear it."""
    temporary = f"{path}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False
    return True
