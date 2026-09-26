"""Blender Copilot - a Copilot-style chat panel that lives inside Blender.

One real turn works end to end: a prompt typed in the panel goes to a worker
subprocess (`transport.py` launches `_worker.py`, which is HTTP and SSE only),
the reply streams back over a pipe, and a `bpy.app.timers` drain repaints the
sidebar as it arrives. The three tools (`run_blender_python`, `get_scene_info`,
`get_rna_info`) are declared and run on Blender's main thread (`toolbox.py`).
History is kept per `.blend` outside the file (`store.py`, `scope.py`), and a turn
that changed the scene leaves one labelled undo step and a receipt that says what
it holds (`undo.py`, `undo_blender.py`).

No `bl_info` here on purpose: as an extension, Blender synthesises `bl_info`
from `blender_manifest.toml` and deletes any hand-written one with a warning.
"""

from __future__ import annotations

import bpy

from . import conversation, panel, scope, stream, transport, undo_blender

_classes = (
    panel.BlenderCopilotPreferences,
    panel.BLENDER_COPILOT_OT_send,
    panel.BLENDER_COPILOT_OT_stop,
    panel.BLENDER_COPILOT_OT_clear,
    panel.BLENDER_COPILOT_OT_toggle_detail,
    panel.BLENDER_COPILOT_OT_compact,
    panel.BLENDER_COPILOT_OT_restore_budget,
    panel.BLENDER_COPILOT_OT_show_code,
    panel.BLENDER_COPILOT_OT_open_transcript,
    panel.BLENDER_COPILOT_OT_enable_global_undo,
    panel.BLENDER_COPILOT_OT_retry_transport,
    panel.BLENDER_COPILOT_OT_reveal_history,
    panel.BLENDER_COPILOT_OT_delete_history,
    panel.BLENDER_COPILOT_PT_panel,
    panel.BLENDER_COPILOT_OT_context_popover,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    # After the classes, because the handlers restore a conversation into the
    # session the panel draws from. The handlers are `persistent` (see
    # `scope.py`): without the decorator a `load_post` one is removed before it
    # can run on the first file load, and the whole scope switch would never fire.
    scope.start()
    # The undo side's change detector, `persistent` for the same measured reason
    # (`undo_blender._on_depsgraph_update`): without the decorator it stops
    # firing after the user's first Open.
    undo_blender.start()


def unregister() -> None:
    undo_blender.stop()
    scope.stop()
    stream.stop()
    # The child's stdin read loop ends on EOF, so an idle orphan is impossible
    # (ticket 11) - but closing it is still ours to do, and doing it politely
    # first means a clean exit rather than a `kill`.
    transport.worker.shutdown()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
