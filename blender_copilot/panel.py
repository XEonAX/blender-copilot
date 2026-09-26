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

from . import (
    budget,
    context,
    conversation,
    prompt,
    scope,
    stream,
    toolbox,
    transport,
    undo,
    undo_blender,
)

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

    # --- the credential (decision 05, ratified) -----------------------------
    # This block is the answer to "where does the API key live": a PASSWORD
    # field on the add-on preferences, which is masked on screen and **plaintext
    # in `userpref.blend`**. 05 §1 accepted that trade out loud, because on macOS
    # the alternative - the environment variable alone - means the user must
    # launch Blender from a terminal forever (launchd does not inherit the
    # shell's environment, measured), and a first run that cannot be fixed from
    # inside Blender is exactly the failure the panel exists to avoid.
    #
    # `subtype="PASSWORD"` is display-layer masking only: Blender draws bullets
    # and refuses to tooltip or copy the value, and Python still reads it
    # verbatim. `options={"SKIP_SAVE"}` does **not** keep it out of the file
    # (measured in 05: the secret appears in `userpref.blend` and survives a
    # restart), so no field here is a safe place for a secret. `draw` says so in
    # the UI rather than implying otherwise.
    api_key: bpy.props.StringProperty(
        name="API key",
        description="Stored unencrypted in Blender's userpref.blend",
        subtype="PASSWORD",
    )

    base_url: bpy.props.StringProperty(
        name="Base URL",
        description=(
            "OpenAI-compatible endpoint, no trailing slash. Empty uses "
            "DEEPSEEK_API_URL, else the provider's documented default"
        ),
    )

    model: bpy.props.StringProperty(
        name="Model",
        description=(
            "Model name. Empty uses DEEPSEEK_MODEL, else deepseek-flash. "
            "A retired name is accepted and silently remapped, so prefer a "
            "name from the provider's own docs"
        ),
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

    newest_first: bpy.props.BoolProperty(
        name="Newest turn first",
        description=(
            "Draw the newest turn directly under the controls instead of at the end "
            "of the transcript. A panel cannot scroll itself, so with this off the "
            "live turn is always a scroll away"
        ),
        default=True,
    )

    # Ticket 14 §1's lever, and the only setting that reads the budget. 0 ships,
    # because the derivation is now the better answer: it is sized from the
    # provider's own window rather than from the 32k floor the design assumed. The
    # range is a way to spend *less* memory - a user pointing this add-on at a
    # smaller model - which is why it stops at 512 KiB rather than at the derived
    # default, and what it buys is earlier, visible trimming instead of a rejected
    # request.
    context_history_kib: bpy.props.IntProperty(
        name="History budget (KiB)",
        description=(
            "Bytes of past conversation the model may be sent, in KiB. "
            "0 derives it from the model's context window (recommended); "
            "8-512 sets it lower"
        ),
        default=0,
        min=0,
        max=context.MAX_BUDGET_KIB,
    )

    def draw(self, context):
        """The settings page, so a human can actually fill the key in.

        Reached from Edit ▸ Preferences ▸ Add-ons ▸ Copilot, which is the route a
        Blender launched from the Finder can take - the panel sidebar is not the
        only way in, because `layout.prop` needs a registered RNA owner and this
        is it.

        The plaintext warning is not boilerplate: `userpref.blend` is mode 0644 in
        `~/Library/Application Support/Blender/5.2/config/`, readable by anything
        that reads that file, and there is no scrub hook on this platform. A user
        who does not want the key on disk leaves the field empty and exports the
        variable instead, and this says so.
        """
        layout = self.layout
        budget = wrap_budget(context)

        box = layout.box()
        box.label(text="API key", icon="LOCKED")
        box.prop(self, "api_key", text="")
        for line in conversation.wrap(
            "Stored unencrypted in Blender's userpref.blend - masked on screen, "
            "plaintext on disk. Leave it empty and export DEEPSEEK_API_KEY "
            "instead if that trade is not acceptable; then the key is never "
            "written by Blender at all.",
            budget,
        ):
            box.label(text=line)

        row = layout.row()
        row.prop(self, "base_url", text="Base URL")
        row = layout.row()
        row.prop(self, "model", text="Model")
        for line in conversation.wrap(
            "Both are non-secret and may be left empty: the URL falls back to "
            "DEEPSEEK_API_URL and then to the provider's documented default, and "
            "the model to DEEPSEEK_MODEL and then to deepseek-flash. Model names "
            "churn, and a retired name is accepted while silently serving "
            "something else - prefer a name copied from the provider's live docs.",
            budget,
        ):
            layout.label(text=line)

        # Which route Send will actually use. This is 05 §2's "never show the key,
        # show a fingerprint" - the last four characters, enough to tell two keys
        # apart while rotating and not enough to be a leak - plus the two sources
        # side by side, because "I set it in the panel and it still says env" is
        # otherwise invisible.
        live = layout.box()
        live.label(text="Send will use", icon="INFO")
        for line in conversation.wrap(transport.config(self).describe(), budget):
            live.label(text=line)


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

        # With the preferences, so a Blender launched from the Finder can send:
        # `transport.config()` with no argument is the env-only path the probes
        # use, and this is the in-Blender one. The problem string names both
        # routes rather than only the shell.
        config = transport.config(settings)
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
        "Stop the reply and the rest of the turn. A tool call that is already "
        "running cannot be stopped by this button - bpy executes on the main "
        "thread and Blender does not pump events while it runs - so the call "
        "ends at its own budget instead. A single long Blender or NumPy call "
        "is only stopped once it returns, and code that catches the interrupt "
        "cannot be stopped at all"
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
        # Re-read the settings rather than assume: a missing key is the most
        # likely reason the worker would not run, and it is better to say so
        # again than to let Send fail silently twice - which is also why this
        # re-reads the preferences, since a user who just filled the field in is
        # exactly who presses Retry.
        config = transport.config(prefs(context))
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
# The trim note's own text is wider than its character count says - `\u2702` and
# `\u2014` are wide glyphs - and it is drawn inside a box, so it wraps a few
# characters earlier than the prose around it. Measured by look, not by formula:
# at the default sidebar width the first line was clipped by about eight
# characters with the shared budget (`logs/context-trim.png`), and this is the
# margin that fixed it.
NOTE_WRAP_INSET = 8
# An error row's first line is drawn with the `ERROR` icon beside it, and the icon
# costs horizontal space that the line's character count does not include.
# MEASURED on screen 2026-09-26, 5.2.2, by `tools/budget_panel_probe.py`: at the
# default sidebar width a 33-character first line next to that icon was middle-
# clipped to "⏱ Stopped after 2.0s \u2014 the co\u2026" while a 36-character line
# *without* an icon (the running note's, in the same run) drew whole - in the one
# row whose entire job is saying what happened. Wrapping early costs a line break;
# wrapping late costs the sentence.
ERROR_ICON_INSET = 5


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
        # Which end of the transcript the newest turn is drawn at. Newest-first
        # ships because a Panel cannot scroll itself: with the controls pinned
        # above the transcript, everything *new* used to be at the far end of an
        # unbounded column, so the answer to "has anything happened?" was a scroll
        # away. Reversing the turns puts the live turn immediately under the
        # controls. Kept switchable because it is a reading-order change and the
        # person who has to live with it should be able to flip it without an edit.
        newest_first = bool(getattr(settings, "newest_first", True))

        self._draw_header(layout, context)
        self._draw_working(layout, context)
        self._draw_transport(layout, budget)
        self._draw_input(layout, context, settings)
        self._draw_actions(layout, context, budget)

        # The receipt describes the NEWEST turn, so it is drawn at whichever end
        # that turn is. It used to sit unconditionally after the transcript, which
        # in newest-first order would strand it at the bottom of an unbounded
        # column - a receipt nobody scrolls to read.
        if newest_first:
            self._draw_receipt(layout, budget)

        variant = settings.layout_variant
        if variant == "log":
            self._draw_log(layout, context, newest_first)
        elif variant == "external":
            self._draw_external(layout, context)
        else:
            self._draw_boxes(layout, context, budget, newest_first)

        # In chronological order, "under the streaming reply" is where this
        # belongs: straight after the transcript, because the transcript ends with
        # the turn the receipt describes.
        if not newest_first:
            self._draw_receipt(layout, budget)
        self._draw_coverage(layout, budget)
        self._draw_history(layout, budget)
        self._draw_variant_picker(layout, settings)

    # -- chrome --------------------------------------------------------------
    def _draw_working(self, layout, context):
        """The "it is working" strip: an arc, what it is doing, and for how long.

        Drawn only while a turn is in flight, and directly under the header rather
        than beside the transcript, because it has to be visible when the answer is
        not. A Panel cannot scroll itself, so the end of an unbounded transcript can
        be an arbitrary distance below the fold - and "is it working or has it
        died?" must not cost a scroll to answer.

        The arc is `progress(type="RING")`, Blender's own busy primitive, driven by
        the **wall clock** (`conversation.ring_sweep`): a frame counted per event
        would freeze during a long think, which is exactly the state this exists to
        announce. `stream._tick` keeps repainting while the turn is in flight, which
        is what turns the changing factor into visible motion.

        The words come from `Conversation.busy_note` and the clock from
        `Conversation.elapsed`, in the module the CPython suite can disagree with -
        so the strip cannot drift from the state it claims to describe.
        """
        session = conversation.session
        if not session.streaming:
            return
        # A fixed-height strip: the arc, the phrase, the clock. The phrase is
        # short and the clock is its own label because Blender middle-clips a
        # label that does not fit, and a clipped "Waiti...g for the model" is
        # worse than a shorter sentence.
        box = layout.box()
        row = box.row(align=True)
        row.progress(factor=conversation.ring_sweep(), type="RING")
        row.label(text=session.busy_note())
        row.label(text=f"{session.elapsed():.0f}s")

    def _draw_header(self, layout, context):
        row = layout.row(align=True)
        row.label(text="prototype", icon="INFO")
        # The status word moves to the working strip while a turn is in flight, and
        # only then: measured on screen, the header read "waiting…" directly above
        # a strip reading "Waiting… 1s", which is the same fact twice and one more
        # place for the two to disagree. It stays here for every other state, where
        # the strip is not drawn at all.
        if not conversation.session.streaming:
            row.label(text=conversation.session.status)

        # Which conversation this is, in the header because it is a property of
        # the whole session rather than of the transcript below - and because
        # "unsaved - session only" is the one thing a user must be told *before*
        # they rely on history being kept. It is short deliberately: Blender
        # middle-clips a label that does not fit, and `store.scope_label`
        # shortens a long basename rather than letting the row do it.
        scope_row = layout.row(align=True)
        scope_row.label(text=scope.header(), icon="FILE_BLEND")
        if conversation.session.trimmed:
            # The trim chip shares the second row rather than the first: three
            # labels on the status row middle-clip "prototype" to "prototy...",
            # which is what happened the first time this was drawn (measured on
            # screen, in `tools/trim_panel_probe.py`) and is exactly the kind of
            # clipping the stub layout cannot see.
            #
            # It is **derived**, never a stored flag (ticket 14 §5): recomputed
            # from the store and the budget on every draw, so a chip that says the
            # model is missing part of the record can neither be stale nor be
            # missed when it is true - which is why it is in the header and not
            # behind the transcript.
            scope_row.label(text="\u2702 trimmed")

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
        """The prompt editor, and the shine that says it is busy.

        VS Code Copilot animates a gradient around its prompt box while a request
        is in flight. A Panel cannot colour or animate a *border* - `layout.alert`
        is the only outline and it is red, which would read as an error - so the
        nearest honest thing is drawn at the editor's own edge: a `progress` bar
        inside the same box, whose fill travels, and only while a turn is in
        flight. `BAR` rather than `RING` because its job is to sit along that edge.

        `conversation.bar_sweep` bounces there and back instead of sweeping: a
        sawtooth fill reads as a progress bar that keeps *nearly* finishing, which
        is a claim about a request's end that nothing here can make.
        """
        box = layout.box()
        box.textbox(
            settings,
            "prompt_text",
            initial_visible_lines=3,
            placeholder="Ask Blender...",
        )
        if conversation.session.streaming:
            box.progress(factor=conversation.bar_sweep(), type="BAR")

    def _draw_actions(self, layout, context, wrap_chars):
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
            # The note is the budget's own copy (`budget.RUNNING_LINES`), because
            # the sentences and the numbers are one contract and a second copy
            # here is how a panel starts promising something the code does not do.
            # It is drawn BEFORE the call starts and cannot be repainted while it
            # runs, so it carries the whole thing up front - what stops, what only
            # stops once it returns, that Stop cannot be delivered, and what to do
            # when the code does not stop at all.
            #
            # Every line is wrapped and inside a box on purpose. Blender
            # MIDDLE-CLIPS a label that does not fit, which is how the first
            # version of this note shipped reading "running code - c...ed until
            # it returns". No headless test can catch that: the stub UILayout
            # counts widgets, not pixels, so text that overflows its width is
            # structurally perfect and visually broken.
            note = layout.box()
            head = note.row()
            head.enabled = False
            head.label(text="running code", icon="TIME")
            for line in budget.RUNNING_LINES:
                for chunk in conversation.wrap(line, wrap_chars):
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
    def _draw_log(self, layout, context, newest_first):
        box = layout.box()
        lines = conversation.session.visible_lines(newest_first=newest_first)
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
    def _draw_boxes(self, layout, context, budget, newest_first):
        # No pager, and none left to switch back to. The human visual pass of
        # 2026-09-26 rejected bounded pages in favour of emitting everything and
        # letting the sidebar REGION scroll - the panel cannot scroll itself, but
        # the region it lives in can. So the whole transcript renders here.
        #
        # The order is `newest_first`'s: the newest turn is drawn first, so the live
        # one sits directly under the controls. Only the *turns* are reversed -
        # `conversation.turns` keeps each turn's own lines in reading order, because
        # a reply drawn above the prompt it answers is an answer to the wrong
        # question.
        messages = conversation.session.messages
        index_of = {id(message): i for i, message in enumerate(messages)}
        grouped = conversation.turns(messages)
        for turn in reversed(grouped) if newest_first else grouped:
            self._draw_turn(layout, turn, index_of, budget)

    def _draw_turn(self, layout, turn, index_of, budget):
        """One exchange: the prompt as a labelled line, then its reply in one box.

        An exchange is one unit (see `conversation.turns`), so the two halves are
        drawn here rather than in two passes - which is what makes the newest-first
        order legible: reversing the exchanges moves a prompt and its answer
        together, and never puts the answer above the question.

        A group that does not start with a prompt is a reply with nothing above it
        (a cleared session, or a conversation read back), and it is drawn as a box
        on its own.
        """
        if turn[0].kind == conversation.KIND_USER:
            layout.label(text="You", icon="USER")
            for chunk in conversation.wrap(turn[0].text, budget):
                layout.label(text=chunk)
            layout.separator(factor=0.4)
            parts = turn[1:]
        else:
            parts = turn

        if not parts:
            return
        turn_box = layout.box()
        for message in parts:
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
        elif message.kind == conversation.KIND_NOTE:
            self._draw_note(box, message, index, budget)
        elif message.kind == conversation.KIND_ERROR:
            box.alert = True
            # WRAP, never one label. Blender middle-clips a label that does not
            # fit instead of wrapping it, so a 104-character error shipped
            # reading "Request failed: ...othing changed." - not a summary of the
            # error but a mangling of it, in the state that most needs to be
            # legible. Every other kind already draws a short title with the full
            # text behind an expander; errors were the lone exception.
            #
            # The first line carries an icon, so it wraps earlier than the prose
            # around it (`ERROR_ICON_INSET`) - measured, not guessed: see the
            # constant. The second and later lines have no icon and would fit at
            # the shared budget, but they are wrapped to the same measure on
            # purpose, because two indents in one paragraph reads as a mistake.
            for position, chunk in enumerate(
                conversation.wrap(
                    message.text, max(MIN_WRAP_CHARS, budget - ERROR_ICON_INSET)
                )
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

    def _draw_note(self, box, message, index, budget):
        """The trim notice (build ticket 06).

        One sentence, wrapped like every other piece of prose - Blender
        middle-clips a label that does not fit, and a note about what the model
        could not see is the worst possible line to mangle. Behind it, an expander
        that says which turns went, because "the model saw less than you did" is
        only trustworthy when the panel can name the part that went.
        """
        for position, chunk in enumerate(
            conversation.wrap(
                message.text, max(MIN_WRAP_CHARS, budget - NOTE_WRAP_INSET)
            )
        ):
            box.label(text=chunk, icon="INFO" if position == 0 else "NONE")
        if not message.detail:
            return
        row = box.row(align=True)
        icon = "TRIA_DOWN" if message.expanded else "TRIA_RIGHT"
        toggle = row.operator(
            "blender_copilot.toggle_detail",
            text=message.purpose or "details",
            icon=icon,
        )
        toggle.index = index
        if message.expanded:
            for line in message.detail.splitlines():
                for chunk in conversation.wrap(
                    line, max(MIN_WRAP_CHARS, budget - NOTE_WRAP_INSET)
                ):
                    box.label(text=chunk)

    def _draw_detail(self, box, message, index):
        row = box.row(align=True)
        icon = "TRIA_DOWN" if message.expanded else "TRIA_RIGHT"
        toggle = row.operator("blender_copilot.toggle_detail", text="output", icon=icon)
        toggle.index = index
        if message.expanded:
            for line in conversation.gutter(message.detail):
                box.label(text=line)
