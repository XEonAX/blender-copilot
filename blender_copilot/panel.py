"""The panel, its operators and its preferences.

Placement is settled: the 3D Viewport sidebar (`VIEW_3D` / `UI`). The layout is
the `boxes` variant *How a conversation is laid out and controlled* chose and a
human looked at; the two rejected variants stay switchable from a preference
because they cost nothing and are the comparison surface for the next visual
pass.

Send is real now. It builds the wire messages (`prompt.py`), hands them to the
worker subprocess (`transport.py`), and `stream.py` repaints as the reply
arrives. Two operator behaviours carry transport policy rather than taste:

  * **Stop** cancels the child and marks the turn stopped in the UI immediately,
    then keeps draining until the child agrees or the grace expires.
  * a worker that **cannot run at all** gets a persistent block with a reason
    and a Retry, and Send is disabled - a turn that cannot start must not look
    like a turn that produced nothing.

The two Text editors are written on change regardless of variant, because the
panel cannot show code unwrapped or selectable. `Open transcript` / `Show code`
are GUI-only and best-effort - they were not exercised headlessly.
"""

from __future__ import annotations

import bpy

from . import conversation, prompt, scope, stream, toolbox, transport, undo, undo_blender

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
    # `texts.new()` defaults `use_fake_user` to True, which *is* what makes a Text
    # datablock survive into the file it is saved in - measured, and the reason the
    # conversation itself is not stored this way. These mirrors are a rendering
    # surface, not a record, so they get no fake user; `scope._on_save_pre` removes
    # them outright on the way into a save, because a Text Editor displaying one
    # holds a real user and would keep it alive regardless.
    text.use_fake_user = False
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
    bl_description = "Send the prompt and stream the reply back into the panel"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        session = conversation.session
        settings = prefs(context)
        if settings is None:
            self.report({"ERROR"}, "Blender Copilot preferences unavailable")
            return {"CANCELLED"}
        if session.streaming:
            self.report({"INFO"}, "A turn is already running")
            return {"CANCELLED"}
        # §4's pause. Asked through `undo_blender` so the banner, this refusal and
        # the disabled button all answer from the same rule - and asked with
        # *this* context, because that is the one the panel is drawing for.
        pause = undo_blender.pause_reason(context)
        if pause:
            self.report(
                {"ERROR"},
                f"{undo.PAUSE_LINES[pause][0]} Auto-run is paused",
            )
            return {"CANCELLED"}

        text = settings.prompt_text.strip()
        if not text:
            self.report({"INFO"}, "Nothing to send")
            return {"CANCELLED"}

        config = transport.config()
        if config.problem:
            session.set_transport_error(config.problem)
            stream.tag_view3d_redraw()
            self.report({"ERROR"}, "Blender Copilot is not configured - see the panel")
            return {"CANCELLED"}

        # Built BEFORE the turn is opened, because the new prompt is not yet part
        # of the transcript - opening the turn first would send the user's own
        # message twice.
        messages = prompt.messages_for(session, text)
        problem = transport.worker.send(config, messages, toolbox.SCHEMAS)
        if problem:
            session.set_transport_error(problem)
            stream.tag_view3d_redraw()
            self.report({"ERROR"}, "The worker could not start - see the panel")
            return {"CANCELLED"}

        session.begin_turn(text)
        # Only cleared on success, so a refused send leaves the text where the
        # user can fix and resend it rather than retyping a paragraph.
        settings.prompt_text = ""
        mirror_transcript()
        stream.start()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_stop(bpy.types.Operator):
    bl_idname = "blender_copilot.stop"
    bl_label = "Stop"
    bl_description = (
        "Stop the in-flight stream and keep what arrived. A running tool call "
        "cannot be stopped: bpy executes on the main thread and Blender does "
        "not pump events while it runs"
    )
    bl_options = {"INTERNAL"}

    def execute(self, context):
        # Stop in the UI now, then tell the child: waiting for the child to
        # agree would leave the button live for up to the cancel grace. The
        # drain keeps pumping until the child acknowledges or is killed.
        conversation.session.cancel()
        transport.worker.cancel()
        mirror_transcript()
        stream.start()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_retry_transport(bpy.types.Operator):
    bl_idname = "blender_copilot.retry_transport"
    bl_label = "Retry"
    bl_description = "Kill any half-started worker and try again on the next send"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        transport.worker.reset()
        conversation.session.set_transport_error(None)
        # Re-read the environment rather than assume: a missing variable is the
        # most likely reason the worker would not run, and it is better to say
        # so again than to let Send fail silently twice.
        config = transport.config()
        if config.problem:
            conversation.session.set_transport_error(config.problem)
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_clear(bpy.types.Operator):
    bl_idname = "blender_copilot.clear"
    bl_label = "Clear"
    bl_description = "End this conversation and start a new one; history is kept"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        # `scope.clear` finalizes rather than deletes: the stored conversation is
        # written out and a fresh one is started in the same scope. Deleting is
        # the separate, confirmed action below.
        scope.clear()
        stream.stop()
        mirror_transcript()
        stream.tag_view3d_redraw()
        return {"FINISHED"}


class BLENDER_COPILOT_OT_reveal_history(bpy.types.Operator):
    bl_idname = "blender_copilot.reveal_history"
    bl_label = "Show history folder"
    bl_description = "Open the folder that holds the stored conversations"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not scope.reveal():
            self.report({"ERROR"}, "There is no history folder to show")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLENDER_COPILOT_OT_delete_history(bpy.types.Operator):
    bl_idname = "blender_copilot.delete_history"
    bl_label = "Delete all chat history"
    bl_description = (
        "Delete every stored conversation. This cannot be undone - the files are "
        "removed from disk"
    )
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        # The 5.2.2 idiom (bundled use: `5.2/scripts/startup/bl_operators/
        # presets.py`). Deleting everything is the one action here that destroys
        # something the user cannot get back, so it asks - and the prompt says what
        # goes, not just "are you sure".
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Delete all chat history?",
            message="Every stored conversation is removed from disk. This cannot be undone.",
            confirm_text="Delete",
            icon="WARNING",
        )

    def execute(self, context):
        removed = scope.delete_all()
        stream.tag_view3d_redraw()
        if not removed:
            self.report({"INFO"}, "There was no stored history to delete")
            return {"FINISHED"}
        self.report({"INFO"}, f"Deleted {removed} stored conversation file(s)")
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

# ---------------------------------------------------------------------------
# Responsive wrapping
#
# A Panel cannot scroll anything but its region, and that region is
# user-resizable - so a FIXED character wrap is wrong by construction. It wastes
# half a widened panel and clips on a narrowed one, and both failure modes were
# seen on screen before this existed.
#
# `PX_PER_CHAR` is measured, not guessed: `blf.dimensions(0, "n" * 64)` at the UI
# font size (11 points, `ui_scale` 1.0) returns 7.0 px/char on Blender 5.2.2 -
# see `tools/panel_width_probe.py`. It scales with `ui_scale`, because the whole
# UI does. `blf` reports whole pixels, so 7.0 is exact only to the pixel, and
# `UI_INSET_PX` is generous for that reason: it is better to wrap a character or
# two early than to clip.
# ---------------------------------------------------------------------------
PX_PER_CHAR = 7.0
UI_INSET_PX = 30.0
MIN_WRAP_CHARS = 12


def wrap_budget(context) -> int:
    """Characters that fit on one line of the panel at its CURRENT width.

    Falls back to `conversation.BOX_WRAP_CHARS` when there is no region, which is
    the case under `tools/panel_draw_smoke.py`: the draw bodies run there against
    a stub layout with no window at all.
    """
    width = getattr(getattr(context, "region", None), "width", 0) or 0
    if width <= UI_INSET_PX:
        return conversation.BOX_WRAP_CHARS
    scale = 1.0
    try:
        scale = float(context.preferences.system.ui_scale) or 1.0
    except Exception:
        pass
    return max(MIN_WRAP_CHARS, int((width - UI_INSET_PX) / (PX_PER_CHAR * scale)))


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
        #
        # One wrap budget per draw, from the region's CURRENT width, shared by
        # every piece of prose below: transcript, receipt and coverage all wrap
        # to the same measure, so a resized sidebar changes all of them together.
        budget = wrap_budget(context)
        self._draw_header(layout, context)
        self._draw_transport(layout, budget)
        self._draw_input(layout, context, settings)
        self._draw_actions(layout, context, budget)

        variant = settings.layout_variant
        if variant == "log":
            self._draw_log(layout, context)
        elif variant == "external":
            self._draw_external(layout, context)
        else:
            self._draw_boxes(layout, context, budget)

        # Drawing the receipt straight after the transcript IS "under the
        # streaming reply": it belongs to the turn it describes rather than
        # standing above the input, which is what the visual pass reversed.
        self._draw_receipt(layout, budget)
        self._draw_coverage(layout, budget)
        self._draw_history(layout, budget)
        self._draw_variant_picker(layout, settings)

    # -- chrome --------------------------------------------------------------
    def _draw_header(self, layout, context):
        row = layout.row(align=True)
        row.label(text="prototype", icon="INFO")
        row.label(text=conversation.session.status)

        # Which conversation this is, in the header because it is a property of
        # the whole session rather than of the transcript below - and because
        # "unsaved - session only" is the one thing a user must be told *before*
        # they rely on history being kept. It is short deliberately: Blender
        # middle-clips a label that does not fit, and `store.scope_label`
        # shortens a long basename rather than letting the row do it.
        scope_row = layout.row(align=True)
        scope_row.label(text=scope.header(), icon="FILE_BLEND")

        pause = undo_blender.pause_reason(context)
        if pause:
            # Wrapped, and each sentence its own label: a bare column clips at a
            # much smaller width than a box does, which is how an earlier note in
            # this file shipped reading "running code - c...ed until it returns".
            # The budget is read here rather than passed in, so this method stays
            # callable with just the context the way `draw` calls it.
            budget = wrap_budget(context)
            banner = layout.box()
            banner.alert = True
            for position, line in enumerate(undo.PAUSE_LINES[pause]):
                for chunk in conversation.wrap(line, budget):
                    banner.label(text=chunk, icon="ERROR" if position == 0 else "NONE")
            action = undo.PAUSE_ACTION[pause]
            if action:
                banner.operator(
                    action, text="Turn Global Undo on", icon="CHECKMARK"
                )

    def _draw_transport(self, layout, budget):
        """Ticket 11's persistent failure state: a reason, a disabled Send, and
        a Retry - never a retry loop.

        Above the input on purpose: it is the one message that changes what the
        controls below it can do, so it must not be reachable only by scrolling
        past an unbounded transcript.
        """
        problem = conversation.session.transport_error
        if not problem:
            return
        box = layout.box()
        box.alert = True
        box.label(text="Send is unavailable", icon="ERROR")
        for line in conversation.wrap(problem, budget):
            box.label(text=line)
        box.operator(
            "blender_copilot.retry_transport", text="Retry", icon="FILE_REFRESH"
        )

    def _draw_receipt(self, layout, budget):
        """The step the last turn created, and what it holds (build ticket 05).

        Drawn straight after the transcript, because the transcript ends with the
        turn this receipt describes. A turn that changed nothing clears it
        (`undo_blender.finish_turn`), so what is on screen is always about a real
        step that exists right now.

        Everything is wrapped: a receipt whose *point* is naming what changed, and
        which Blender middle-clips, names nothing.
        """
        receipt = conversation.session.last_receipt
        if not receipt:
            return
        box = layout.box()
        if not receipt["undoable"]:
            box.alert = True
        head = box.row(align=True)
        head.label(
            text=receipt["title"],
            icon="CHECKMARK" if receipt["undoable"] else "ERROR",
        )
        for line in receipt["changed"]:
            for chunk in conversation.wrap(line, budget):
                box.label(text=chunk)
        if receipt["more"]:
            box.label(text=f"and {receipt['more']} more")
        for line in receipt["lines"]:
            for chunk in conversation.wrap(line, budget):
                box.label(text=chunk)

    def _draw_input(self, layout, context, settings):
        layout.textbox(
            settings,
            "prompt_text",
            initial_visible_lines=3,
            placeholder="Ask Blender...",
        )

    def _draw_actions(self, layout, context, budget):
        session = conversation.session
        row = layout.row(align=True)
        paused = bool(undo_blender.pause_reason(context))
        if session.streaming:
            # Stop replaces Send in the same slot, so its position never moves.
            row.operator("blender_copilot.stop", text="Stop", icon="PAUSE")
        else:
            sub = row.row(align=True)
            # Disabled when auto-run is paused, and when there is no transport to
            # send through. A Send that cannot work must not look pressable - and
            # the pause is `undo_blender`'s answer here, not a second reading of
            # the preference, so the button and the banner cannot disagree.
            sub.enabled = not paused and not session.transport_error
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
            # Wrapped like every other piece of prose. These lines were hard-coded
            # single labels, and the owner's NARROW screenshot caught the 38
            # characters of the third one overflowing a ~295 px sidebar as
            # "A Python loop can, with a call ...". Fixed strings are not exempt
            # from the width.
            for line in (
                "Click waits until the call returns.",
                "A blocking C call never stops.",
                "A Python loop can, with a call budget.",
            ):
                for chunk in conversation.wrap(line, budget):
                    note.label(text=chunk)

    def _draw_coverage(self, layout, budget):
        box = layout.box()
        # These are the honesty contract's sentences. They must be readable at any
        # sidebar width, so they wrap like everything else instead of clipping.
        for line in conversation.COVERAGE_LINES:
            for chunk in conversation.wrap(line, budget):
                box.label(text=chunk)

    def _draw_history(self, layout, budget):
        """Where the conversation is filed, and the two actions that touch the
        record itself.

        The scope name is repeated from the header because this is where the
        destructive button is, and "what am I about to delete" should not require
        scrolling back to the top of an unbounded transcript. The note is either a
        retention admission - the cap pruned turns, and a store that prunes
        silently is a trust bug - or the session-only warning, and it wraps because
        both are sentences rather than labels.
        """
        box = layout.box()
        heading = box.row(align=True)
        heading.label(text=scope.header(), icon="FILE_BLEND")
        note = scope.note()
        if note:
            for line in conversation.wrap(note, budget):
                box.label(text=line)
        row = box.row(align=True)
        row.operator("blender_copilot.reveal_history", text="Show folder", icon="FILE_FOLDER")
        row.operator("blender_copilot.delete_history", text="Delete all", icon="TRASH")

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
    def _draw_boxes(self, layout, context, budget):
        # No pager, and none left to switch back to. The human visual pass of
        # 2026-09-26 rejected bounded pages in favour of emitting everything and
        # letting the sidebar REGION scroll - the panel cannot scroll itself, but
        # the region it lives in can. So the whole transcript renders here.
        messages = conversation.session.messages
        index_of = {id(message): i for i, message in enumerate(messages)}
        for turn in _turns(messages):
            self._draw_turn(layout, turn, index_of, budget)

    def _draw_turn(self, layout, turn, index_of, budget):
        """A user turn is a labelled block; an assistant turn is one bordered
        box holding its whole reply - prose, code and tool calls together."""
        first = turn[0]
        if first.kind == conversation.KIND_USER:
            layout.label(text="You", icon="USER")
            for chunk in conversation.wrap(first.text, budget):
                layout.label(text=chunk)
            layout.separator(factor=0.4)
            return

        turn_box = layout.box()
        for message in turn:
            self._draw_part(turn_box, message, index_of.get(id(message), -1), budget)

    def _draw_part(self, layout, message, index, budget):
        if message.kind == conversation.KIND_ASSISTANT:
            if message.text:
                for chunk in conversation.wrap(message.text, budget):
                    layout.label(text=chunk)
            return

        box = layout.box()
        if message.kind == conversation.KIND_CODE:
            self._draw_code(box, message, index)
        elif message.kind == conversation.KIND_TOOL:
            self._draw_tool(box, message, index)
        elif message.kind == conversation.KIND_ERROR:
            box.alert = True
            # WRAP, never one label. Blender middle-clips a label that does not
            # fit instead of wrapping it, so a 104-character error shipped
            # reading "Request failed: ...othing changed." - not a summary of the
            # error but a mangling of it, in the state that most needs to be
            # legible. Every other kind already draws a short title with the full
            # text behind an expander; errors were the lone exception.
            for position, chunk in enumerate(
                conversation.wrap(message.text, budget)
            ):
                box.label(text=chunk, icon="ERROR" if position == 0 else "NONE")
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
    """Group the transcript into turns: a user message starts one, assistant
    parts join."""
    turns = []
    for message in messages:
        if message.kind == conversation.KIND_USER or not turns:
            turns.append([message])
        elif turns[-1][0].role == "assistant":
            turns[-1].append(message)
        else:
            turns.append([message])
    return turns
