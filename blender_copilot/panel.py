"""The panel, its operators and its preferences.

Placement is settled: the 3D Viewport sidebar (`VIEW_3D` / `UI`). This revision
carries the *How a conversation is laid out and controlled* prototype: three
candidate layouts over one conversation model, switchable at runtime from a
preference so a human can compare them in the GUI.

  * `log`      - flat prefixed text, inline everything, cheapest chrome.
  * `boxes`    - user turn as a labelled block, assistant turn as a bordered
                 box with collapsed code / tool rows. The chosen layout.
  * `external` - panel keeps controls only; transcript and code live in
                 addon-owned Text datablocks in a Text Editor.

The two Text editors are written on change regardless of variant, because the
panel cannot show code unwrapped or selectable. `Open transcript` / `Show code`
are GUI-only and best-effort - they were not exercised headlessly.
"""

from __future__ import annotations

import bpy

from . import conversation, stream

CATEGORY = "Copilot"
NON_HOST_AREAS = {"TEXT_EDITOR", "PREFERENCES", "STATUSBAR", "TOPBAR"}


def prefs(context) -> "BlenderCopilotPreferences | None":
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


# ---------------------------------------------------------------------------
# Companion Text datablocks. Cheap, always available, and the only surface
# where code is legible and selectable.
# ---------------------------------------------------------------------------

def write_text(name: str, body: str):
    text = bpy.data.texts.get(name)
    if text is None:
        text = bpy.data.texts.new(name)
    text.clear()
    text.write(body)
    return text


def mirror_code(message: conversation.Message):
    return write_text(conversation.CODE_TEXT, message.detail)


def mirror_transcript():
    return write_text(conversation.TRANSCRIPT_TEXT, conversation.session.transcript_text())


def focus_text_datablock(context, text) -> bool:
    """Best-effort: show `text` in a Text Editor, splitting one if needed.

    NOT verified headlessly - it needs a screen with areas. Every step is
    guarded so a failure just leaves the datablock in the Text Editor's list.
    """
    window = getattr(context, "window", None)
    screen = getattr(window, "screen", None) if window else None
    if screen is None:
        return False

    for area in screen.areas:
        if area.type == "TEXT_EDITOR":
            if area.spaces.active is not None:
                area.spaces.active.text = text
            area.tag_redraw()
            return True

    target = None
    for area in screen.areas:
        if area.type in NON_HOST_AREAS:
            continue
        if target is None or area.width * area.height > target.width * target.height:
            target = area
    if target is None:
        return False
    region = next((r for r in target.regions if r.type == "WINDOW"), None)
    if region is None:
        return False

    before = {area.as_pointer() for area in screen.areas}
    try:
        with context.temp_override(window=window, screen=screen, area=target, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=0.5)
    except Exception:
        return False

    new_area = next(
        (area for area in screen.areas if area.as_pointer() not in before), None
    )
    if new_area is None:
        return False
    try:
        new_area.type = "TEXT_EDITOR"
        if new_area.spaces.active is not None:
            new_area.spaces.active.text = text
        new_area.tag_redraw()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

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

    layout_variant: bpy.props.EnumProperty(
        name="Layout",
        description="Prototype only: which conversation layout to render",
        items=(
            ("boxes", "Role boxes", "Labelled turns, boxed assistant replies"),
            ("log", "Flat log", "Prefixed plain text, no borders"),
            ("external", "External transcript", "Controls only; transcript in a Text datablock"),
        ),
        default="boxes",
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

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
        if not context.preferences.edit.use_global_undo:
            self.report({"ERROR"}, "Global Undo is off - auto-run is paused")
            return {"CANCELLED"}
        if not settings.prompt_text.strip():
            self.report({"INFO"}, "Nothing to send")
            return {"CANCELLED"}
        conversation.session.send(settings.prompt_text)
        settings.prompt_text = ""
        mirror_transcript()
        stream.start()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_stop(bpy.types.Operator):
    bl_idname = "blender_copilot.stop"
    bl_label = "Stop"
    bl_description = (
        "Stop the in-flight stream. A running tool call cannot be stopped: "
        "bpy is executing on the main thread and Blender does not pump events"
    )
    bl_options = {"INTERNAL"}

    def execute(self, context):
        conversation.session.cancel()
        stream.stop()
        mirror_transcript()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_clear(bpy.types.Operator):
    bl_idname = "blender_copilot.clear"
    bl_label = "Clear"
    bl_description = "Discard the conversation"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        conversation.session.clear()
        stream.stop()
        mirror_transcript()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_toggle_detail(bpy.types.Operator):
    bl_idname = "blender_copilot.toggle_detail"
    bl_label = "Toggle detail"
    bl_description = "Show or hide a code block, tool output or traceback"
    bl_options = {"INTERNAL"}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        conversation.session.toggle(self.index)
        mirror_transcript()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_show_code(bpy.types.Operator):
    bl_idname = "blender_copilot.show_code"
    bl_label = "Show code"
    bl_description = "Copy this code into the Copilot Code Text datablock and open an editor"
    bl_options = {"INTERNAL"}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        index = self.index
        if not 0 <= index < len(conversation.session.messages):
            self.report({"ERROR"}, "Message is gone")
            return {"CANCELLED"}
        text = mirror_code(conversation.session.messages[index])
        if not focus_text_datablock(context, text):
            self.report({"INFO"}, f'Code is in the "{conversation.CODE_TEXT}" text datablock')
        return {"FINISHED"}


class BLENDER_COPILOT_OT_open_transcript(bpy.types.Operator):
    bl_idname = "blender_copilot.open_transcript"
    bl_label = "Open transcript"
    bl_description = "Put the full transcript in a Text Editor"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        text = mirror_transcript()
        if not focus_text_datablock(context, text):
            self.report({"INFO"}, f'Transcript is in the "{conversation.TRANSCRIPT_TEXT}" datablock')
        return {"FINISHED"}


class BLENDER_COPILOT_OT_page_older(bpy.types.Operator):
    bl_idname = "blender_copilot.page_older"
    bl_label = "Older"
    bl_description = "Show earlier turns"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        conversation.session.older()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_page_newer(bpy.types.Operator):
    bl_idname = "blender_copilot.page_newer"
    bl_label = "Newer"
    bl_description = "Show later turns"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        conversation.session.newer()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_enable_global_undo(bpy.types.Operator):
    bl_idname = "blender_copilot.enable_global_undo"
    bl_label = "Turn Global Undo on"
    bl_description = "Restore the only mechanism that can revert an agent turn"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        context.preferences.edit.use_global_undo = True
        stream.tag_view3d_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

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

        # Panel order carries more weight here than usual. A Panel cannot scroll
        # (only the region can) and the transcript is now unbounded, so the
        # controls sit ABOVE it and never drift off the fold, and everything the
        # transcript displaces lives below it.
        # Human visual pass, 2026-09-26: the owner rejected the bounded pager in
        # favour of emitting everything, and separately reported having to scroll
        # the sidebar to reach Send because the input sat below the transcript.
        self._draw_header(layout, context)
        self._draw_input(layout, context, settings)
        self._draw_actions(layout, context)

        variant = settings.layout_variant
        if variant == "log":
            self._draw_log(layout, context)
        elif variant == "external":
            self._draw_external(layout, context)
        else:
            self._draw_boxes(layout, context)

        # Drawing the receipt straight after the transcript IS "under the
        # streaming reply": it belongs to the turn it describes rather than
        # standing above the input, which is what the visual pass reversed.
        self._draw_receipt(layout)
        self._draw_coverage(layout)
        self._draw_variant_picker(layout, settings)

    # -- chrome --------------------------------------------------------------
    def _draw_header(self, layout, context):
        row = layout.row(align=True)
        row.label(text="prototype", icon="INFO")
        row.label(text=conversation.session.status)

        if not context.preferences.edit.use_global_undo:
            banner = layout.box()
            banner.alert = True
            banner.label(text="Global Undo is off.", icon="ERROR")
            banner.label(text="Nothing the agent runs can be undone.")
            banner.label(text="Auto-run is paused.")
            banner.operator(
                "blender_copilot.enable_global_undo",
                text="Turn Global Undo on",
                icon="CHECKMARK",
            )

    def _draw_receipt(self, layout):
        receipt = conversation.session.last_receipt
        if not receipt:
            return
        box = layout.box()
        row = box.row(align=True)
        row.label(text="Undoable", icon="CHECKMARK")
        box.label(text=receipt["summary"])
        box.label(text=receipt["coverage"])

    def _draw_input(self, layout, context, settings):
        layout.textbox(
            settings,
            "prompt_text",
            initial_visible_lines=3,
            placeholder="Ask Blender...",
        )

    def _draw_actions(self, layout, context):
        session = conversation.session
        row = layout.row(align=True)
        undo_on = context.preferences.edit.use_global_undo
        if session.streaming:
            # Stop replaces Send in the same slot, so its position never moves.
            row.operator("blender_copilot.stop", text="Stop", icon="PAUSE")
        else:
            sub = row.row(align=True)
            sub.enabled = undo_on
            sub.operator("blender_copilot.send", text="Send", icon="PLAY")
        row.operator("blender_copilot.clear", text="Clear", icon="TRASH")

        if session.running_tool is not None:
            # Name both cases rather than claiming a blanket "cannot be
            # interrupted" (measured in ticket 17: SIGALRM stops `while True:
            # pass`, `time.sleep` and a blocking recv at 1.04x overhead, and
            # cannot stop a long native call until it returns).
            #
            # Every line is short and inside a box on purpose. Blender
            # MIDDLE-CLIPS a label that does not fit, which is how the first
            # version of this note shipped reading "running code - c...ed until
            # it returns". No headless test can catch that: the stub UILayout
            # counts widgets, not pixels, so text that overflows its width is
            # structurally perfect and visually broken.
            note = layout.box()
            head = note.row()
            head.enabled = False
            head.label(text="running code", icon="TIME")
            note.label(text="Click waits until the call returns.")
            note.label(text="A blocking C call never stops.")
            note.label(text="A Python loop can, with a call budget.")

    def _draw_coverage(self, layout):
        box = layout.box()
        for line in conversation.COVERAGE_LINES:
            box.label(text=line)

    def _draw_variant_picker(self, layout, settings):
        box = layout.box()
        box.label(text="Layout (prototype)", icon="OPTIONS")
        box.prop(settings, "layout_variant", text="")

    # -- variant: flat log ---------------------------------------------------
    def _draw_log(self, layout, context):
        box = layout.box()
        lines = conversation.session.visible_lines()
        if lines:
            for line in lines:
                box.label(text=line)
        else:
            box.label(text="No conversation yet.")

    # -- variant: external transcript ---------------------------------------
    def _draw_external(self, layout, context):
        box = layout.box()
        box.label(text="Transcript lives in a Text Editor.", icon="TEXT")
        box.label(text='"Copilot Transcript" - full history,')
        box.label(text="selectable and searchable.")
        box.operator("blender_copilot.open_transcript", text="Open transcript", icon="FILE_SCRIPT")
        box.label(text=f'Code: "{conversation.CODE_TEXT}"')

    # -- variant: role boxes (chosen) ---------------------------------------
    def _draw_boxes(self, layout, context):
        page, hidden, has_older, has_newer = conversation.session.page_view()
        if hidden or has_newer:
            pager = layout.row(align=True)
            older = pager.row(align=True)
            older.enabled = has_older
            older.operator("blender_copilot.page_older", text="Older", icon="LOOP_BACK")
            newer = pager.row(align=True)
            newer.enabled = has_newer
            newer.operator("blender_copilot.page_newer", text="Newer", icon="LOOP_FORWARDS")
            if hidden:
                pager.label(text=f"{hidden} earlier")

        index_of = {id(message): i for i, message in enumerate(conversation.session.messages)}
        for turn in _turns(page):
            self._draw_turn(layout, turn, index_of)

    def _draw_turn(self, layout, turn, index_of):
        """A user turn is a labelled block; an assistant turn is one bordered
        box holding its whole reply - prose, code and tool calls together."""
        first = turn[0]
        if first.kind == conversation.KIND_USER:
            layout.label(text="You", icon="USER")
            for chunk in conversation.wrap(first.text, conversation.BOX_WRAP_CHARS):
                layout.label(text=chunk)
            layout.separator(factor=0.4)
            return

        turn_box = layout.box()
        for message in turn:
            self._draw_part(turn_box, message, index_of.get(id(message), -1))

    def _draw_part(self, layout, message, index):
        if message.kind == conversation.KIND_ASSISTANT:
            if message.text:
                for chunk in conversation.wrap(message.text, conversation.BOX_WRAP_CHARS):
                    layout.label(text=chunk)
            return

        box = layout.box()
        if message.kind == conversation.KIND_CODE:
            self._draw_code(box, message, index)
        elif message.kind == conversation.KIND_TOOL:
            self._draw_tool(box, message, index)
        elif message.kind == conversation.KIND_ERROR:
            box.alert = True
            box.label(text=message.text, icon="ERROR")
            if message.detail:
                self._draw_detail(box, message, index)

    def _draw_code(self, box, message, index):
        line_count = len(message.detail.splitlines())
        row = box.row(align=True)
        icon = "TRIA_DOWN" if message.expanded else "TRIA_RIGHT"
        toggle = row.operator(
            "blender_copilot.toggle_detail",
            text=f"{message.purpose or 'code'} \u00b7 {line_count} lines",
            icon=icon,
        )
        toggle.index = index
        show = row.operator("blender_copilot.show_code", text="", icon="FILE_SCRIPT")
        show.index = index
        if message.expanded:
            for line in conversation.gutter(message.detail):
                box.label(text=line)
        else:
            box.label(text="\u2192 full code in \u201cCopilot Code\u201d", icon="INFO")

    def _draw_tool(self, box, message, index):
        status = message.status
        if status == conversation.STATUS_RUNNING:
            icon = "TIME"
        elif status == conversation.STATUS_ERROR:
            icon = "ERROR"
            box.alert = True
        else:
            icon = "CHECKMARK"
        row = box.row(align=True)
        row.label(text=message.purpose or message.text, icon=icon)
        if message.detail:
            self._draw_detail(box, message, index)
        elif status == conversation.STATUS_RUNNING:
            row.label(text="running\u2026")

    def _draw_detail(self, box, message, index):
        row = box.row(align=True)
        icon = "TRIA_DOWN" if message.expanded else "TRIA_RIGHT"
        toggle = row.operator("blender_copilot.toggle_detail", text="output", icon=icon)
        toggle.index = index
        if message.expanded:
            for line in conversation.gutter(message.detail):
                box.label(text=line)


def _turns(messages):
    """Group the page into turns: a user message starts one, assistant parts join."""
    turns = []
    for message in messages:
        if message.kind == conversation.KIND_USER or not turns:
            turns.append([message])
        elif turns[-1][0].role == "assistant":
            turns[-1].append(message)
        else:
            turns.append([message])
    return turns
