"""The panel itself: where the chat will live.

Placement is settled: the 3D Viewport sidebar (`VIEW_3D` / `UI`), because the
region scrolls - a Panel cannot - and because it is where bundled add-ons put
theirs. Nothing here calls a model yet.
"""

from __future__ import annotations

import bpy

from . import conversation, stream

CATEGORY = "Copilot"


def prefs(context) -> "BlenderCopilotPreferences | None":
    """Resolve our preferences, or None if somehow drawn without them."""
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


class BlenderCopilotPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    # The textbox needs an RNA-backed string to bind to. Preferences are the
    # right scope for a per-user draft, and are NOT encrypted - Blender writes
    # them into its user preference file. Nothing secret goes here.
    prompt_text: bpy.props.StringProperty(
        name="Prompt",
        description="Draft prompt",
        options={"TEXTEDIT_UPDATE"},
    )


class BLENDER_COPILOT_OT_send(bpy.types.Operator):
    bl_idname = "blender_copilot.send"
    bl_label = "Send"
    bl_description = "Send the prompt (prototype: replays a canned reply)"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = prefs(context)
        if settings is None:
            self.report({"ERROR"}, "Blender Copilot preferences unavailable")
            return {"CANCELLED"}
        if not settings.prompt_text.strip():
            self.report({"INFO"}, "Nothing to send")
            return {"CANCELLED"}
        conversation.session.send(settings.prompt_text)
        settings.prompt_text = ""
        stream.start()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_clear(bpy.types.Operator):
    bl_idname = "blender_copilot.clear"
    bl_label = "Clear"
    bl_description = "Discard the conversation"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        conversation.session.clear()
        stream.stop()
        return {"FINISHED"}


class BLENDER_COPILOT_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Blender Copilot"

    def draw(self, context):
        layout = self.layout
        settings = prefs(context)
        if settings is None:
            layout.label(text="Preferences unavailable", icon="ERROR")
            return

        header = layout.row(align=True)
        header.label(text="prototype", icon="INFO")
        header.label(text=conversation.session.status)

        # The transcript. A Panel cannot scroll, so this is a truncated view
        # rather than a scrollable one - see the panel-mechanics findings.
        box = layout.box()
        lines = conversation.session.visible_lines()
        if lines:
            for line in lines:
                box.label(text=line)
        else:
            box.label(text="No conversation yet.")

        layout.textbox(
            settings,
            "prompt_text",
            initial_visible_lines=3,
            placeholder="Ask Blender...",
        )

        row = layout.row(align=True)
        row.operator("blender_copilot.send", text="Send", icon="PLAY")
        row.operator("blender_copilot.clear", text="Clear", icon="TRASH")
