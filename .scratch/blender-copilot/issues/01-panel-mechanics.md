# What can a Blender 5.2 panel actually render and accept?

Type: research
Status: resolved
Blocked by: none

## Question

What are the real capabilities and limits of a `bpy.types.Panel` as the surface for a chat UI in Blender 5.2, and which space/region should host it?

Answer at least:

1. **Live text.** `Panel.draw()` renders once per redraw. What forces a repaint from Python — `region.tag_redraw()`, `bpy.ops.wm.redraw_timer`, something else? Can a `bpy.app.timers` callback drive it? Cite the C source for the tagging/invalidation functions.
2. **Streaming.** Can text that grows token-by-token be rendered acceptably, or does a panel repaint the whole `draw()` on every update in a way that flickers or is too costly?
3. **Long content.** Can a panel scroll arbitrary-length content? What do `UILayout`, `bpy.types.UIList`, and `layout.box()` actually permit? Is there any scrollable text region available to an addon, or does long history force a different surface (Text Editor text datablock, separate window/area)?
4. **Multi-line input.** What can an addon offer for typing a *multi-line* prompt? `layout.prop` on a StringProperty, `bl_property`/search popups, a modal operator, or a Text Editor area used as the input? Report what is genuinely possible.
5. **Placement.** Given that an addon cannot register a new space type, compare the viable hosts for this panel — 3D Viewport sidebar (`VIEW_3D` / `UI`, `bl_category`), Properties tabs, Text Editor, Node Editor — on available width, always-visible-ness, and precedent in bundled addons.
6. **Prior art.** Does any existing open-source addon already implement a chat-like or streaming-text panel inside Blender? If so, what does it do and is it reusable?

Facts already established (do not re-derive): `bpy` is main-thread-only; `bpy.app.timers` and modal operators pump only from the GUI event loop and never under `blender -b`; addons cannot register a new space type; the target is **installed Blender 5.2.2**, and `/Users/user/Projects/blender` (5.3.0-alpha) is read-only reference.

## Answer

**Host the panel in the 3D Viewport sidebar** (`VIEW_3D` / `UI`, with a `bl_category`) — user-resizable, and the region scrolls, which a panel itself cannot do. It is also where bundled addons already put panels.

- **Repaint** from a `bpy.app.timers` callback that ends in `region.tag_redraw()`. Do **not** use `bpy.ops.wm.redraw_timer`: it blocks the main thread and is documented as a hack not intended for inclusion.
- **Multi-line input exists.** `UILayout.textbox(data, property, initial_visible_lines, placeholder, ...)` — verified against 5.2.2's own RNA table, described as *"a text-box widget with multi-line support"*. Bind it to an addon-owned `StringProperty`. This is the prompt input; no modal-operator workaround needed.
- **Hard limits.** A `Panel` cannot scroll (`UILayout` has no scroll function, and `layout.box()` is only a bordered sub-layout) — only the region, a `template_list`, a `textbox`, a Text Editor area, or the Console scrollback scroll. `draw()` re-runs and rebuilds the whole block on every repaint, so there is **no incremental text append** — cost is O(widgets) per frame in Python.
- **No rich text.** Verified: `UILayout.label` accepts only `text`/`text_ctxt`/`translate`/`icon`/`icon_value` — no markup. Code and tool output cannot be styled in-panel. A Text Editor area bound to an addon-owned `Text` datablock (which auto-notifies on write) is the fallback for long history and possibly for code display.

Detail and citations: `research/blender-panel-mechanics.md`.

**Caveat on the research file:** its claim that text is not rich is confirmed, but treat any *other* claim in it that turns on a 5.2-vs-5.3 API difference with suspicion — the researcher had the 5.3.0-alpha clone open, and version mis-attribution is the obvious failure mode.

## Comments
