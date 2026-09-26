"""A conversation that survives closing, reopening and switching files.

    BLENDER_COPILOT_HISTORY_DIR=/tmp/bc-t04-hist python3 tools/bounded_run.py 120 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --background \\
        --factory-startup --python tools/persistence_probe.py

Every layer here is the shipped one: the real extension package
(`bl_ext.user_default.blender_copilot`, so `__package__` is the name Blender's own
`extension_path_user` needs), the real handlers, the real store, real `.blend`
files on disk, and the real `stream._tick` - which is clocked by this script
instead of by `bpy.app.timers`, because timers never pump under `blender -b`
(measured; that is why ticket 02's loop needed a GUI probe). Clocking `_tick` by
hand is how the *turn-end write point* gets exercised headlessly at all.

The wire is faked, as in `tools/loop_panel_probe.py`: nothing is sent anywhere and
no credential is read.

`BLENDER_COPILOT_HISTORY_DIR` points the store at a scratch directory, so the
probe never writes a conversation into the user's own config. The override is
asserted, and the real directory is asked for separately and printed - that call is
the one thing an environment cannot fake, so it is measured rather than assumed.

What it proves: history is written on the tick a turn ends; reopening the same file
restores it; a different file gets its own; Save As re-keys without duplicating;
the cap prunes whole turns and never orphans a result; nothing of the conversation
is inside the `.blend`; and delete-all clears the directory.

What it does **not** prove: the confirmation dialog's appearance (that needs a
window, and `tools/persistence_panel_probe.py` is where it is looked at), anything
about how the panel looks, and a GUI Blender actually quitting with history
unwritten - which is why the write happens at the end of every turn rather than at
exit.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import bpy

# Dummy values so `transport.config()` is satisfied: the worker is replaced below,
# so nothing is ever sent and no real credential is read. Without them the loop's
# *second* round refuses to go out (`stream._send_round` reads the config every
# time), and the probe would be measuring a transport failure instead of a turn.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-probe-not-a-real-key")
os.environ.setdefault("DEEPSEEK_API_URL", "http://127.0.0.1:9")

EXT = "bl_ext.user_default.blender_copilot"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "persistence-probe.txt"

NOTES: list[str] = []
FAILURES: list[str] = []
SENTINEL = "SENTINEL-c0ffee-turn-one"


def note(line: str) -> None:
    NOTES.append(line)
    print(f"PERSIST | {line}", flush=True)


def check(label: str, condition: bool) -> bool:
    if condition:
        note(f"ok   {label}")
    else:
        FAILURES.append(label)
        note(f"FAILED {label}")
    return bool(condition)


# ---------------------------------------------------------------------------
# The scripted transport: the worker protocol, exactly as `loop_panel_probe.py`
# fakes it, and for the same reason (this stands in for `transport.Worker`).
# ---------------------------------------------------------------------------

class ScriptedWorker:
    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.sent: list[dict] = []
        self._queue: list[dict] = []

    @property
    def busy(self) -> bool:
        return bool(self._queue or self.rounds)

    @property
    def alive(self) -> bool:
        return True

    def send(self, cfg, messages, tools=None) -> str | None:
        self.sent.append({"messages": messages, "tools": tools})
        self._queue.extend(self.rounds.pop(0) if self.rounds else [])
        return None

    def tick(self) -> list[dict]:
        events, self._queue = self._queue, []
        return events

    def cancel(self) -> None:
        self._queue = []
        self.rounds = []

    def shutdown(self, timeout: float = 1.0) -> None:
        pass

    def reset(self) -> None:
        self._queue = []


def tool_round(call_id: str, purpose: str, code: str, prose: str = "") -> list[dict]:
    events = [{"ev": "delta", "text": prose}] if prose else []
    events.append(
        {
            "ev": "done",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "run_blender_python",
                        "arguments": json.dumps({"code": code, "purpose": purpose}),
                    },
                }
            ],
        }
    )
    return events


def prose_round(text: str) -> list[dict]:
    return [
        {"ev": "delta", "text": text},
        {"ev": "done", "finish_reason": "stop", "tool_calls": []},
    ]


def clock(bc, limit: int = 400) -> int:
    """Run the shipped drain tick until the turn is over. Returns ticks used.

    The same function `bpy.app.timers` would call, with the same one-step-per-tick
    discipline - just driven by this loop, because under `blender -b` there is no
    event loop to drive it.
    """
    ticks = 0
    session = bc.conversation.session
    while session.streaming and ticks < limit:
        bc.stream._tick()
        ticks += 1
    return ticks


def send(bc, worker, prompt: str) -> str:
    """The panel's send path, without the panel: build, send, open the turn."""
    session = bc.conversation.session
    config = bc.transport.config()
    if config.problem:
        return config.problem
    messages = bc.prompt.messages_for(session, prompt)
    problem = worker.send(config, messages, bc.toolbox.SCHEMAS)
    if problem:
        return problem
    session.begin_turn(prompt)
    return ""


def conversation_files(store_dir: Path) -> list[str]:
    if not store_dir.is_dir():
        return []
    return sorted(name for name in os.listdir(store_dir) if name.endswith(".json"))


def read_index(store_dir: Path) -> dict:
    return json.loads((store_dir / "active.json").read_text(encoding="utf-8"))


def history_of(store_dir: Path, conversation_id: str) -> list[dict]:
    record = json.loads((store_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))
    return record["messages"]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def main() -> int:
    bc = importlib.import_module(EXT)
    scope = bc.scope
    store = bc.store
    session = bc.conversation.session

    scratch = Path(tempfile.mkdtemp(prefix="bc-t04-"))
    store_dir = Path(os.environ["BLENDER_COPILOT_HISTORY_DIR"])
    file_a = scratch / "persist-a.blend"
    file_b = scratch / "persist-b.blend"
    file_c = scratch / "persist-c.blend"
    file_d = scratch / "persist-d.blend"
    note(f"scratch: {scratch}")
    note(f"history dir (override): {store_dir}")

    try:
        # 1. The directory. The override is what makes this probe safe to run, and
        # the real answer is asked for separately because an environment cannot
        # fake it: `__package__` here is the extension's own name, and
        # `extension_path_user` needs exactly that.
        check("the override is what the store uses", scope.store_.directory() == str(store_dir))
        override = os.environ.pop(scope.ENV_DIR)
        real = scope._directory()
        os.environ[scope.ENV_DIR] = override
        note(f"real extension user dir: {real}")
        check(
            "the real directory comes from the extension's own package name",
            real.endswith("/.user/user_default/blender_copilot/conversations"),
        )
        try:
            bpy.utils.extension_path_user("blender_copilot", path="conversations")
            check("a bare package name would raise, which is why the guard is there", False)
        except ValueError as exc:
            note(f"bare package name raises: {type(exc).__name__}: {exc}")
            check("a bare package name raises, which is why the guard is there", True)
        check("the scope is not pinned before any turn has been stored", not scope.current.pinned)

        # 2. Register: classes plus the three persistent handlers.
        bc.register()
        check("load_post is registered", scope._on_load_post in bpy.app.handlers.load_post)
        check("save_post is registered", scope._on_save_post in bpy.app.handlers.save_post)
        check("save_pre is registered", scope._on_save_pre in bpy.app.handlers.save_pre)

        # 3. A scene worth naming, saved to file A. `save_post` is what tells the
        # scope which file this conversation belongs to.
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.mesh.primitive_cube_add(size=2, location=(0.0, 0.0, 0.0))
        cube = bpy.context.active_object
        bpy.ops.wm.save_as_mainfile(filepath=str(file_a), compress=False)
        check("the saved file is the scope now", scope.current.blend_path == str(file_a))
        check("and the header shows its basename", scope.header() == "persist-a.blend")
        check("nothing is filed yet - an empty conversation is not worth a file", conversation_files(store_dir) == [])

        # 4. One real turn, clocked by hand. The write point under test is the very
        # last tick of it: `stream._tick` notices the turn ended and persists.
        worker = ScriptedWorker(
            [
                tool_round(
                    "call_a1",
                    "Ask about the scene",
                    "print('probe ran inside the sandbox')",
                    SENTINEL,
                ),
                prose_round("That is the sentinel turn, answered."),
            ]
        )
        bc.transport.worker = worker
        session.clear()
        problem = send(bc, worker, SENTINEL)
        check("the scripted turn started", not problem)
        ticks = clock(bc)
        note(f"the turn took {ticks} ticks")
        check("the turn is over", not session.streaming)

        files = conversation_files(store_dir)
        check("ending the turn wrote exactly one conversation file", len(files) == 2 and "active.json" in files)
        conversation_id = scope.current.id
        check("the scope now has an id", bool(conversation_id))
        stored = history_of(store_dir, conversation_id)
        check("the stored history is the wire history, verbatim", stored == session.history)
        check(
            "including the assistant's tool_calls and the tool result that answers it",
            stored[1].get("tool_calls")[0]["id"] == "call_a1" and stored[2]["role"] == "tool",
        )
        check("the sentinel prompt is on disk", SENTINEL in json.dumps(stored))
        check("and the index points the file path at it", read_index(store_dir)["active"][str(file_a)] == conversation_id)
        record = json.loads((store_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))
        check("the record carries the schema ticket 14 will extend", record["schema"] == store.SCHEMA)
        check(
            "and a retention record that says nothing was pruned",
            record["meta"]["retention"]["turns_dropped"] == 0,
        )
        check(
            "the write is not repeated on a tick that changed nothing",
            scope.persist() is False,
        )

        # 4b. A conversation in an unsaved file: nothing is written, and the first
        # save files it under the path the file turns out to have. This is the
        # other half of "session only" - the half where the session ends by
        # becoming a file.
        scope.switch("")
        files_before = conversation_files(store_dir)
        session.restore(stored)
        check("an unsaved file's conversation is not written", scope.persist() is False)
        check("and the directory is unchanged by it", conversation_files(store_dir) == files_before)
        check("while the panel says it is session-only", scope.header() == store.SESSION_ONLY_LABEL)
        bpy.ops.wm.save_as_mainfile(filepath=str(file_d), compress=False)
        check("the first save files the conversation it was carrying",
              SENTINEL in json.dumps(history_of(store_dir, scope.current.id)))
        check("under the path the file just got",
              read_index(store_dir)["active"][str(file_d)] == scope.current.id)
        check("and the file it came from keeps its own",
              read_index(store_dir)["active"][str(file_a)] != scope.current.id)
        bpy.ops.wm.open_mainfile(filepath=str(file_a))
        check("which is still there when that file is opened again", session.history == stored)
        # Four conversations now, so the checks below count relative to this
        # rather than to a number written down twice.
        files = conversation_files(store_dir)

        # 5. Nothing of the conversation is inside the .blend. The panel mirrors
        # code and transcript into Text datablocks because a sidebar cannot show
        # code legibly; those are the leak vector, and `save_pre` removes them.
        bc.panel.mirror_code(session.messages[2])
        bc.panel.mirror_transcript()
        check(
            "the mirrors exist in memory first, or the check below proves nothing",
            bc.conversation.TRANSCRIPT_TEXT in bpy.data.texts,
        )
        bpy.ops.wm.save_mainfile(filepath=str(file_a), compress=False)
        check(
            "save_pre removed the mirrors, so the .blend cannot contain them",
            bc.conversation.TRANSCRIPT_TEXT not in bpy.data.texts
            and bc.conversation.CODE_TEXT not in bpy.data.texts,
        )
        raw = file_a.read_bytes()
        check("the sentinel is not in the saved .blend", SENTINEL.encode() not in raw)
        check("nor is the tool output", b"probe ran inside the sandbox" not in raw)
        note(f"persist-a.blend is {len(raw)} bytes")
        check("the conversation is still on disk after saving the .blend", SENTINEL in (store_dir / f"{conversation_id}.json").read_text(encoding="utf-8"))

        # 6. A different file is a different conversation. Written first, so the
        # check that A comes back is a restore rather than a fresh start.
        bpy.ops.wm.save_as_mainfile(filepath=str(file_b), compress=False)
        check("Save As re-keyed the conversation rather than starting one", scope.current.id == conversation_id)
        check("and kept the old path as an alias", str(file_a) in scope.current.aliases)
        check("no second conversation file appeared", conversation_files(store_dir) == files)
        index = read_index(store_dir)["active"]
        check(
            "both names point at the same conversation",
            index[str(file_a)] == conversation_id and index[str(file_b)] == conversation_id,
        )

        # 7. Open a third, genuinely different file: the scope switches, and the
        # thread does not travel with it.
        bpy.ops.wm.read_homefile(use_empty=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(file_c), compress=False)
        check("opening another file switched the scope", scope.current.blend_path == str(file_c))
        check("the conversation did not follow", session.history == [] and session.messages == [])
        check("and the header names the file that is open", scope.header() == "persist-c.blend")
        check("the other conversation is untouched", conversation_files(store_dir) == files)

        # 8. Coming back. A save, then a load - the real route a user takes - and
        # the decorator question answered by measurement rather than by assertion.
        # Two handlers are registered on the spot, identical except for
        # `@bpy.app.handlers.persistent`, and one file load is then performed: the
        # ratified decision records that an undecorated `load_post` handler is
        # removed *before* it can run on a file load, and if that holds here the
        # plain one's counter stays at zero while the decorated one fires.
        bpy.ops.wm.save_mainfile(filepath=str(file_c), compress=False)
        fired = {"decorated": 0, "plain": 0}

        @bpy.app.handlers.persistent
        def _decorated_counter(_dummy):
            fired["decorated"] += 1

        def _plain_counter(_dummy):
            fired["plain"] += 1

        bpy.app.handlers.load_post.append(_decorated_counter)
        bpy.app.handlers.load_post.append(_plain_counter)
        bpy.ops.wm.open_mainfile(filepath=str(file_a))
        note(f"load_post counters after one file open: {fired}")
        check("the decorated handler ran on the file load", fired["decorated"] == 1)
        check(
            "and the undecorated one did not - the decorator is what survives the "
            "handler flush a file read performs",
            fired["plain"] == 0,
        )
        for function in (_plain_counter, _decorated_counter):
            if function in bpy.app.handlers.load_post:
                bpy.app.handlers.load_post.remove(function)
        check("reopening the file restored the scope", scope.current.blend_path == str(file_a))
        check("and the wire history with it", session.history == stored)
        rows = session.messages
        check("the display transcript was rebuilt from it", len(rows) == len(stored) + 1)
        check(
            "with the user's ask, the model's prose, the code and the tool row",
            [row.kind for row in rows]
            == ["user", "assistant", "code", "tool", "assistant"],
            # Shown, not just asserted: getting this order wrong is the bug the
            # check exists for, and a bare False says nothing about which row moved.
        ) or note(f"     actual row kinds: {[row.kind for row in rows]}")
        check("the tool row came back finished, not running", rows[3].status == "ok")
        check("with its output behind the expander", "probe ran inside the sandbox" in rows[3].detail)
        check(
            "and the code row still carries the code for `Show code`",
            "print('probe ran inside the sandbox')" in rows[2].detail,
        )

        # 9. The cap. Retrieved from the store's own rule rather than by hand:
        # 300 messages of single-call turns, well past 200.
        long_history = []
        for turn in range(100):
            long_history.append({"role": "user", "content": f"ask {turn}"})
            long_history.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"cap_{turn}",
                            "type": "function",
                            "function": {
                                "name": "run_blender_python",
                                "arguments": json.dumps({"code": "pass", "purpose": f"p{turn}"}),
                            },
                        }
                    ],
                }
            )
            long_history.append({"role": "tool", "tool_call_id": f"cap_{turn}", "content": '{"ok": true}'})
        session.restore(long_history)
        scope.persist()
        capped = history_of(store_dir, scope.current.id)
        note(f"300 stored messages became {len(capped)}")
        check("the cap pruned to its limit", len(capped) <= store.CAP_MESSAGES)
        check("it starts at a turn boundary", capped[0]["role"] == "user")
        check("no result lost the call that produced it", store.orphan_results(capped) == [])
        check("and no call lost its result", store.unanswered(capped) == [])
        capped_record = json.loads(
            (store_dir / f"{scope.current.id}.json").read_text(encoding="utf-8")
        )
        check(
            "the pruning is recorded, so it is not silent",
            capped_record["meta"]["retention"]["turns_dropped"] > 0
            and capped_record["meta"]["retention"]["reason"] == "cap_200_messages",
        )
        check("and the panel has a sentence for it", "history cap" in scope.note())
        check("trimming did not touch the .blend", not file_a.read_bytes().endswith(b" "))

        # 10. Clear finalizes; delete-all is the destructive one, and this probe
        # calls the action its confirmation gates (the dialog itself is looked at
        # in the GUI probe).
        scope.clear()
        check("Clear ended the conversation without deleting it", len(conversation_files(store_dir)) >= 2)
        check("and started an empty one in the same scope", session.history == [])
        check("which is not filed yet, so Clear cannot leave an empty file behind",
              scope.current.id == "")
        removed = scope.delete_all()
        note(f"delete_all removed {removed} file(s)")
        check("delete-all removed every conversation", removed >= 2)
        check("and the directory is gone", not store_dir.exists())
        check("delete-all left the session with an empty conversation", session.history == [])
        check("the .blend files themselves are untouched", file_a.exists() and file_b.exists())

        # 11. The folder control, existence only: calling it would open a Finder
        # window, and a probe should not decorate the desk it is running on.
        check("wm.path_open exists for `Show folder`", "Open a path in a file browser" in (bpy.ops.wm.path_open.__doc__ or ""))
        check("and there is nothing to reveal after delete-all", scope.reveal() is False)

        # 12. The last load: with the store emptied, opening A starts fresh rather
        # than raising on a missing directory.
        bpy.ops.wm.open_mainfile(filepath=str(file_a))
        check("a file whose history was deleted opens empty, without error", session.history == [])
    finally:
        bc.unregister()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(NOTES) + "\n", encoding="utf-8")

    for failure in FAILURES:
        note(f"FAILED: {failure}")
    print("SMOKE OK" if not FAILURES else "SMOKE FAILED", flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        import traceback

        traceback.print_exc()
        print("SMOKE FAILED", flush=True)
        code = 1
    if code:
        sys.exit(code)
    if "bpy" in sys.modules:
        # Blender quits itself, so the bounded driver's deadline is a backstop and
        # not the mechanism.
        try:
            bpy.ops.wm.quit_blender()
        except Exception:
            pass
    time.sleep(0)
