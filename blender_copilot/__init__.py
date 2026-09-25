"""Blender Copilot - a Copilot-style chat panel that lives inside Blender.

Prototype stage. The panel renders a canned conversation and a fake streaming
reply; no model is called, no tool is executed, nothing is persisted. Its job
is to prove the panel is a viable surface before an agent loop is built on it.

No `bl_info` here on purpose: as an extension, Blender synthesises `bl_info`
from `blender_manifest.toml` and deletes any hand-written one with a warning.
"""

from __future__ import annotations

import bpy

from . import conversation, panel, stream

_classes = (
    panel.BlenderCopilotPreferences,
    panel.BLENDER_COPILOT_OT_send,
    panel.BLENDER_COPILOT_OT_clear,
    panel.BLENDER_COPILOT_PT_panel,
)


def register() -> None:
    conversation.seed_once()
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    stream.stop()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
