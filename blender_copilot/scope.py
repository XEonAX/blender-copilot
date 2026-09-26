"""Which conversation the session is showing, and the handlers that keep it so.

`store.py` knows how a conversation is filed; this module is the part that has to
ask Blender where, and has to notice when the answer changes. Three things live
here and nowhere else:

  * **The directory.** `bpy.utils.extension_path_user(__package__, path=
    "conversations", create=True)`. Two measured traps shape the call: it *raises*
    `ValueError` when the package name does not name an extension (so a plain
    `import blender_copilot`, as the headless checks do, must degrade rather than
    crash), and it returns `""` when it cannot create the directory. Either way the
    answer is "nowhere to write", and the panel says so instead of pretending.
  * **The scope switches.** `load_post` says which file the session is now in,
    `save_post` says Save As happened, and both are decorated
    `@bpy.app.handlers.persistent`. That decorator is not tidiness: a `load_post`
    handler without it is removed *before* it can run on the first file load, so it
    never fires - measured, and it would make this ticket silently do nothing.
  * **The write point.** History only changes during a turn, so `stream.py` calls
    `persist()` on the tick a turn ends: at most one small file per turn, and
    nothing to write when nothing changed.

`save_pre` is here for the same reason the whole ticket is: nothing of the
conversation may end up inside a `.blend`. The panel mirrors code and transcript
into `Text` datablocks because a sidebar cannot show code legibly, and a `Text`
datablock with a user *is* written into the file and *does* travel with it
(measured; that is why history is not stored that way). So the mirrors are removed
on the way into a save.

`BLENDER_COPILOT_HISTORY_DIR` overrides the directory. It exists so a probe can run
the real code against a real directory without writing probe conversations into
the user's own config - the same trick `tools/capability_probe.py` uses with
`BLENDER_USER_CONFIG`.
"""

from __future__ import annotations

import os

import bpy

from . import conversation, store, transport, undo_blender

ENV_DIR = "BLENDER_COPILOT_HISTORY_DIR"


def _directory() -> str:
    """The conversations directory, or `""` when there is not one."""
    override = (os.environ.get(ENV_DIR) or "").strip()
    if override:
        return override
    try:
        path = bpy.utils.extension_path_user(
            __package__, path=store.CONVERSATIONS_DIR, create=True
        )
    except Exception:
        # Not an extension package: an add-on loaded by file path, or a bare
        # module in the headless checks. `extension_path_user` raises here rather
        # than returning "", which is measured, so both are handled.
        return ""
    return path or ""


def _filepath() -> str:
    """`bpy.data.filepath`, or `""` where `bpy.data` will not say.

    During registration `bpy.data` is a `_RestrictData` with no `filepath` at all -
    measured the hard way, in a GUI session where the extension could not register.
    An extension that fails to register is far worse than a scope that starts out
    empty, so this asks defensively rather than assuming.
    """
    try:
        return bpy.data.filepath or ""
    except AttributeError:
        return ""


store_ = store.Store(_directory)
current = store.Scope(store_)

# The history as it was last written, so a save that would change nothing is not
# made. `None` means "this conversation has never been written", which is a
# different thing from "it was written when it was empty".
_written: list[dict] | None = None


# ---------------------------------------------------------------------------
# The session's scope
# ---------------------------------------------------------------------------

def header() -> str:
    """The header's short name for where this conversation lives."""
    return current.label


def note() -> str:
    """One line about the record itself, or "" when there is nothing to say."""
    dropped = int((current.meta.get("retention") or {}).get("turns_dropped") or 0)
    if dropped:
        turns = "turn" if dropped == 1 else "turns"
        return (
            f"{dropped} earlier {turns} were removed by the history cap; "
            "the file keeps what it can."
        )
    if current.session_only:
        return "This file is unsaved, so nothing is written to disk yet."
    if not store_.available:
        return "There is nowhere to save history, so it lasts this session only."
    return "Kept per .blend file, outside the file itself."


def switch(blend_path: str) -> bool:
    """Point the session at the conversation filed under `blend_path`.

    Called from `load_post` and once at registration. The in-flight turn belonged
    to the file being left, so it is stopped, the worker is reset (so no more of it
    can arrive) and the generation is fenced (so anything already queued cannot
    land in the arriving conversation). Then the old conversation is written and
    the new one is loaded - and no line about any of it is added to either
    transcript, because the store is the provider's wire format.
    """
    global current, _written
    if blend_path == current.blend_path and blend_path:
        # The same file, already open. Reloading it from disk here would throw away
        # the live conversation for a copy of its older self.
        return False

    transport.worker.reset()
    # Before the turn is abandoned, and therefore before `_end_turn` writes the
    # turn's undo step: that turn belongs to the file being left, and a push
    # judged in the next tick would record the *new* file's state under its
    # label. Undo cannot follow a load anyway - loading discards the stack.
    undo_blender.forget()
    conversation.session.abandon()
    persist()

    current, history = store_.open(blend_path)
    conversation.session.restore(history, current.meta)
    _written = conversation.session.snapshot()
    return True


def persist(force: bool = False) -> bool:
    """Write the current conversation. True when a file was written."""
    global _written
    history = conversation.session.snapshot()
    if not force and history == _written:
        return False
    # Ticket 14 §5's informational block, beside `retention`: it records that the
    # model was shown less than the file holds, and is never read back as the
    # source of truth for the projection - that is always recomputed.
    if conversation.session.trim_meta:
        current.meta["context_trim"] = conversation.session.trim_meta
    if not current.save(history):
        return False
    _written = history
    return True


def clear() -> bool:
    """End this conversation and start a fresh one in the same scope.

    "Clear means finalize, not delete" (*Where chat history lives* §4): the file
    keeps what the user had, and the new conversation is empty until they say
    something worth keeping. A turn in flight is stopped first, so the conversation
    left behind is sendable rather than ending on an unanswered call.
    """
    global current, _written
    if conversation.session.streaming:
        transport.worker.reset()
        conversation.session.abandon()
    persist()

    current = store.Scope(store_, current.blend_path)
    conversation.session.clear()
    _written = []
    return True


def delete_all() -> int:
    """Remove every stored conversation. Returns how many files went.

    Only ever reached through the operator that asks first: nothing here is a
    confirmation.
    """
    global current, _written
    removed = store_.delete_all()

    current = store.Scope(store_, current.blend_path)
    conversation.session.clear()
    _written = []
    return removed


def reveal() -> bool:
    """Show the history folder in the OS file browser. True when it was opened.

    Requires the folder to *exist* - not merely to be creatable. After a
    delete-all there is nothing to reveal, and opening an empty folder that this
    call had just recreated would be a lie about what is on disk.
    """
    directory = store_.directory(create=False)
    if not directory or not os.path.isdir(directory):
        return False
    try:
        bpy.ops.wm.path_open(filepath=directory)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _on_load_post(_dummy) -> None:
    switch(_filepath())


@bpy.app.handlers.persistent
def _on_save_post(_dummy) -> None:
    """A save: the first one of an unsaved file, a plain save, or Save As.

    `adopt` re-keys instead of duplicating - the conversation keeps its id and its
    file, and the old path becomes an alias - so both names still find this thread.
    The write is forced when the path changed even though the history did not: the
    conversation's *content* is unchanged but its identity is not, and it is the
    index entry that makes the new name find it.
    """
    blend_path = _filepath()
    if not blend_path:
        return
    adopted = current.adopt(blend_path)
    persist(force=adopted)


@bpy.app.handlers.persistent
def _on_save_pre(_dummy) -> None:
    """Nothing of the conversation goes into the `.blend`.

    The panel's `Text` mirrors are the leak vector: a datablock with a user is
    written into the file and travels with it, which is exactly why the history is
    not stored that way. Removing them here means the file never contains them,
    whatever the user does with the Text Editor afterwards.
    """
    for name in (conversation.CODE_TEXT, conversation.TRANSCRIPT_TEXT):
        text = bpy.data.texts.get(name)
        if text is not None:
            bpy.data.texts.remove(text)


_HANDLERS = (
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.save_post, _on_save_post),
    (bpy.app.handlers.save_pre, _on_save_pre),
)


def start() -> None:
    for handlers, function in _HANDLERS:
        if function not in handlers:
            handlers.append(function)
    # The file may already be open: handlers registered now did not see it load,
    # so the opening state is taken once, here, rather than assumed to be empty.
    switch(_filepath())


def stop() -> None:
    for handlers, function in _HANDLERS:
        if function in handlers:
            handlers.remove(function)
