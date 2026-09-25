# Panel mechanics: what a `bpy.types.Panel` can render/accept, and where to host a chat UI

Type: research
Target: **installed Blender 5.2.2** (`/Applications/Blender.app`, built 2026-09-15).

Citation convention used throughout:

- `[installed]` = `/Applications/Blender.app/Contents/Resources/5.2/...` (the real target).
- `[clone]` = `/Users/user/Projects/blender/...` = **5.3.0-alpha** reference tree, read-only. Used for C
  source. Behaviour claimed here is stated as clone truth; where it matters for 5.2.2 the Python-visible
  surface was re-verified on the installed binary instead (next bullet).
- `[runtime]` = introspection executed on the installed 5.2.2 binary
  (`Blender --factory-startup -b --python-expr ...`), i.e. fact for the target version.
- `[docs]` = official Blender Python API documentation, `https://docs.blender.org/api/5.2/...`.

`scripts/startup/` and `scripts/addons_core/` citations are from the clone (5.3.0-alpha) unless marked
`[installed]`; where a bundled addon's 5.2.2 copy was read directly it says so. Nothing in the
`bl_pkg` (extension-update) citations is inferred — those were read from the 5.2.2 install, and the exact
lines reproduced below were also confirmed by search over the checkout.

Line numbers are from the tree named. Ranges prefixed `~` are approximate.

---

## Recommendation

Host the chat panel in the **3D Viewport sidebar** (`bl_space_type='VIEW_3D'`, `bl_region_type='UI'`,
`bl_category="…"`): it is where every bundled AI/asset addon already lives, it is user-resizable, and the
region scrolls as a whole, so a growing history is reachable without any custom drawing.
Drive streaming with a `bpy.app.timers` callback that ends by calling `region.tag_redraw()` — never
`bpy.ops.wm.redraw_timer` — and take multi-line prompt entry from `layout.textbox()` (new in 5.2), which
is the only widget that edits and scrolls wrapped text inside a panel.
For long/complete history, pair the panel with a Text Editor area bound to an addon-owned `Text`
datablock (native scrolling, selection, copy; redraw is notifier-driven) or a `template_list` UIList,
since a `Panel` itself has no scroll.

---

## 1. What forces a panel repaint from Python

### The mechanism: `ARegion::runtime->do_draw` flags

Every repaint is a bit set on the region, consumed by the window manager draw pass:

- `Region.tag_redraw()` → `ED_region_tag_redraw()` `[clone]`
  `source/blender/editors/screen/area.cc:681-693`. Sets `RGN_DRAW`, clears
  `RGN_DRAW_PARTIAL | RGN_DRAW_NO_REBUILD | RGN_DRAW_EDITOR_OVERLAYS`, sets `do_ime`, resets `drawrct`.
  Guarded by `!(do_draw & RGN_DRAWING)` — i.e. tagging *during* a draw is dropped (the comment there says
  this guard exists precisely because "python scripts can cause this to happen indirectly").
  RNA wrapper: `rna_screen.cc:639` `[clone]`.
- `Area.tag_redraw()` → `ED_area_tag_redraw()` `[clone]` `area.cc:784-791` (loops all regions of the area).
  RNA wrapper: `rna_screen.cc:480` `[clone]`.
- Related taggers, for completeness: `ED_region_tag_redraw_no_rebuild()` `area.cc:727-737`,
  `ED_region_tag_redraw_partial()` `area.cc:757-781`, `ED_region_tag_redraw_editor_overlays()`
  `area.cc:745-755`, `ED_area_tag_redraw_regiontype()` `area.cc:802-812`. Of these, only
  `ED_region_tag_refresh_ui()` (`area.cc:738-743`, flag `RGN_REFRESH_UI` = `DNA_screen_types.h:804`) has a
  Python wrapper, and that wrapper is **restricted to `TEMPORARY` (popup) regions**:
  `rna_Region_tag_refresh_ui()` reports an error for anything else `[clone]` `rna_screen.cc:408-415`.
  The 5.2 API docs list both methods on `Region` `[docs]`
  `https://docs.blender.org/api/5.2/bpy.types.Region.html` (`Region.tag_redraw()`, `Region.tag_refresh_ui()`).

### Where the flag turns into a repaint

`[clone]`:

- `wm_draw_update_test_window()` `wm_draw.cc:1590-1665` returns "draw" if any visible region has
  `do_draw` set.
- `wm_draw_update()` `wm_draw.cc:1691-1745` draws those windows (`wm_draw_window` → per-region
  `ED_region_do_draw`, `area.cc:515`).
- Layout (i.e. panel `draw()` bodies) runs only for regions that are flagged:
  `wm_draw.cc:1004-1010` calls `ED_region_do_layout()` (`area.cc:495-513`) when
  `region.runtime->do_draw` is set and the region type has a `layout` callback.
- The GUI loop that reaches all of this is `WM_main()` `wm.cc` — `wm_window_events_process` →
  `wm_event_do_handlers` → `wm_event_do_notifiers` → `wm_draw_update(C)` at `wm.cc:644`.

### Can a `bpy.app.timers` callback drive it? Yes — in the GUI, in the same loop iteration

- Python timer registration wraps `BLI_timer_register(...)` `[clone]`
  `source/blender/python/intern/bpy_app_timers.cc` (`bpy_app_timers_register`, ~89-118; the returned
  float reschedules, `None` unregisters, `persistent=True` survives file loads — docstring at ~75-88).
- Timers execute from `wm_event_timers_execute()` `[clone]` `wm_event_system.cc:566-578`, whose only
  context setup is `CTX_wm_window_set(C, wm->windows.first())` (`:572-573`) before `BLI_timer_execute()`
  (`:577`). It is called from `wm_event_do_notifiers()` (`:596`) at `:603` — i.e. **between event
  handling and `wm_draw_update()`** in `WM_main`. A timer that tags a region is therefore drawn in that
  same loop pass.
- Consequence for code: inside a timer callback there is a window but **no meaningful `bpy.context.area` /
  `region`**. Find regions by walking `bpy.data.window_managers[*].windows[*].screen.areas[*].regions[*]`
  (or `bpy.context.window_manager.windows`) and keep references, as the shipped code does.

### In-tree proof (shipped in 5.2.2): extension-update status streaming

`[installed]` `scripts/addons_core/bl_pkg/bl_extension_notify.py`:

- `_ui_refresh_apply()` (`:455-470`) tags the Preferences WINDOW region redraw at `:467`, then calls
  `_region_refresh_registered()` (`:470`).
- `_region_refresh_registered()` (`:572-582`) calls `region.tag_redraw()` **and**
  `region.tag_refresh_ui()` (`:577-578`) on a set of regions it remembers.
- That set is fed by `update_ui_region_register(region)`, called from the operator that runs the update:
  `bl_extension_ops.py:241-250`, where `region = context.region_popup` — a `TEMPORARY` region, which is
  why the `tag_refresh_ui()` restriction in §1 is not violated. First tag happens at `:246-247`, and the
  `draw()` method re-tags at `:263-264`.
- The timer is registered at `:599-600` (`bpy.app.timers.register(..., first_interval=…, persistent=True)`),
  and returns a delay to poll again.

This is a real, shipped "streaming status text in a panel/popup driven by `bpy.app.timers` +
`region.tag_redraw()`" implementation of the exact pattern we need.

### Other things that force a repaint

- **Property writes observed through the message bus.** The region subscribes to its space's RNA at the
  end of `region_do_draw()` `[clone]` `area.cc:637-660` (`ED_region_do_msg_notify_tag_redraw`,
  defined at `area.cc:394`), so a property update notified on the bus tags the region redraw without an
  explicit `tag_redraw()`. (Blender still adds explicit `tag_redraw()` calls where the notify path is not
  guaranteed — see the bl_pkg code above.)
- **Notifiers from other subsystems.** E.g. Text Editor areas redraw on `NC_TEXT | NA_EDITED`
  (`[clone]` `space_text.cc:162-192`); `bpy.ops.console.scrollback_append` tags its area with
  `ED_area_tag_redraw(area)` (`console_ops.cc:1132`).
- **`bpy.ops.wm.redraw_timer`** `[clone]` `wm_operators.cc:3767-3970`: types `DRAW`, `DRAW_SWAP`,
  `DRAW_WIN`, `DRAW_WIN_SWAP`, `ANIM_STEP`, `ANIM_PLAY`, `UNDO` (`:3792-3802`); it forces
  draw + buffer swap (`:3767-3778`) and loops `iterations` times (`:3893-3950`). Poll requires
  `!G.background && WM_operator_winactive(C)` (`:3885-3891`) — so it is GUI-only, and it **blocks the main
  thread** for the whole run. It is exposed in the UI as a benchmark menu
- `[clone]` `scripts/startup/bl_ui/space_topbar.py:365` (`layout.operator_menu_enum("wm.redraw_timer", "type")`), under the Topbar's benchmarking menu.

### What the official docs say about forcing redraw

`[clone]` `doc/python_api/rst/info_gotchas_internal_data_and_python_objects.rst:180-211` (published in the
API docs under *Info → Gotchas*):

> "Tools that lock Blender in a loop redraw are highly discouraged since they conflict with Blender's
> ability to run multiple operators at once and update different parts of the interface as the tool runs."
> … "So the solution here is to write a **modal** operator" … and for the redraw hack itself:
> "scripts that use this hack will not be considered for inclusion in Blender and any issue with using it
> will not be considered a bug", showing `bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)`.

So: `tag_redraw()` from a timer = supported design; `wm.redraw_timer` = explicitly discouraged hack.

---

## 2. Can growing/streaming text be rendered acceptably?

**Yes for a token/step-rate stream; the panel's whole layout is rebuilt on every repaint, so cost is
O(all widgets in the panel) per frame, evaluated in Python.**

Evidence that `draw()` re-runs and the block is rebuilt from scratch:

- `ed_panel_draw()` `[clone]` `area.cc:3229-3390` creates a `ui::Block` per panel per layout pass
  (`block_begin`, `:3245`) and calls `pt->draw(C, panel)` at `area.cc:3358` — i.e. the Python `draw()` body
  runs on **every** layout pass, for every panel in the region.
- A layout pass happens whenever the region is flagged `do_draw` `[clone]` `wm_draw.cc:1004-1010`.
- Each pass allocates a **new** `Block` for the same panel name; the old one is marked inactive
  (`block_region_set()` `interface.cc:4052-4060`: "each listbase only has one block with this name, free
  block if is already there so it can be rebuilt from scratch"), with only a `buttons_ptrs` capacity
  reservation as reuse (`block_begin()` `interface.cc:4104-4130`), and the inactive block is freed after
  the draw via `blocklist_free_inactive(C, region)` (`area.cc:603`, definition `interface.cc:4002-4018`).

Consequences:

- There is **no incremental text update path**: streaming means rewriting the layout each repaint. Cost
  scales with the number of widgets you emit, so render a *fixed-size tail* (e.g. last N lines) rather than
  the whole transcript.
- Flicker: nothing in the pipeline is partially updated within a panel — the region is drawn from its own
  buffer and swapped — so a full re-layout should not tear. I did not measure this on a live 5.2.2 with a
  large history (see *What I could not determine*). The one thing that *does* visibly jump is region
  scroll position and panel geometry when content height changes.
- Throttle: the timer's return value is the delay to the next call (`bpy_app_timers.cc`, docstring
  `first_interval`/return-float semantics), so pick a rate (e.g. 0.1-0.25 s) instead of redrawing per token.
- Blender's own attitude to per-`draw()` work is documented on the `UIList` page: "iterating over all
  vertices in a 'draw' function is a very bad idea for UI performance!" `[docs]`
  `https://docs.blender.org/api/5.2/bpy.types.UIList.html` (Advanced UIList Example comments).
- Precedent that this is acceptable for ~a few updates/second: bl_pkg streams progress text into a
  popup/preferences region on a timer for the whole duration of a repository update
  (§1, `[installed]` `bl_extension_notify.py:462-582`).

---

## 3. Can a panel scroll arbitrary-length content?

### What a panel can do

- **No scroll API on `UILayout`.** The 5.2.2 function list contains ~110 layout functions
  (`box`, `column`, `row`, `split`, `grid_flow`, `label`, `prop`, `template_*`, …) and its properties are
  `alignment`, `direction`, `scale_x/y`, `emboss`, `enabled`, `active`, `use_property_split`, `ui_units_x/y`
  — none of them scroll [`runtime`].
- **`layout.box()`** is only a nested sub-layout with a border: "Sublayout (items placed in this sublayout
  are placed under each other in a column and are surrounded by a box)" [`runtime`]; implementation
  `Layout::box()` `[clone]` `interface_layout.cc:5315`.
- **The region scrolls, not the panel.** Side-panel regions are View2D-backed:
  `ED_region_panels_init()` `[clone]` `area.cc:4006-4026` calls
  `view2d_region_reinit(&region->v2d, ui::V2D_COMMONVIEW_PANELS_UI, …)` and sets
  `V2D_SCROLL_VERTICAL`; `ED_region_panels_draw()` `area.cc:3916-3990` draws the scrollers
  (`ui::view2d_scrollers_draw`, `:3988`). Panel-category tab offset is kept in `region->category_scroll`
  `[clone]` `interface_panel.cc:1680-1686`, `:1726`. Practical effect: a very long panel stack is
  reachable by scrolling the whole sidebar; scroll resets to the top on category change
  (`interface_panel.cc:1472`, `:2693`).
- **`template_list` (UIList) is internally scrollable.** It keeps its own `ui_list->list_scroll`, clamps it
  to `height - actual_rows` and auto-scrolls to the active item
  `[clone]` `interface_template_list.cc:555-580`, and draws its own scrollbar button
  (`ButtonType::Scroll`, `V2D_SCROLL_WIDTH`) at `:799-814`. So a UIList can present an arbitrary-length,
  scrollable list of history rows inside a panel — but each row is a widget row, not wrapped prose.
- **`UILayout.textbox()` is internally scrollable and multi-line** (new in 5.2). It renders only
  `visible_lines` lines and adds a scrollbar + drag grip when the text is longer
  `[clone]` `interface_widgets.cc:2186-2506` (line slice at `:2458-2472`, grip at `:2475-2487`,
  scrollbar at `:2492-2506`); wheel scrolling routes to `textbox_add_scroll()`
  (`interface_handlers.cc:4329`, `:4603`, `:5477`; implementation `buttons/interface_textbox.cc:51-55`);
  vertical resize by dragging the grip sets `BUTTON_STATE_TEXTBOX_RESIZING`
  (`interface_handlers.cc:245`, `:5545`). Height is `line_height * visible_lines + 2*padding`, floored at
  `UI_UNIT_Y` (`buttons/interface_textbox.cc:353-358`), with `textbox_minimum_visible_lines = 1`
  (`buttons/interface_textbox.hh:25`).

### If a panel cannot scroll: the alternative surfaces available to an addon

| Surface | Scroll? | Notes / citations |
| --- | --- | --- |
| `template_list` (UIList) | yes (own window + scrollbar) | `[clone]` `interface_template_list.cc:555-580`, `:799-814` |
| `layout.textbox` (StringProperty) | yes (own window + scrollbar + wheel) | `[clone]` `interface_widgets.cc:2458-2506`; editable widget, so it is an input surface, not a read-only log |
| Text Editor area + `Text` datablock | yes, native (full editor) | writes notify + redraw: `Text.write()`/`clear()` call `WM_main_add_notifier(NC_TEXT \| NA_EDITED, text)` `[clone]` `rna_text_api.cc:26-36`; the space listener tags redraw and rebuilds the draw cache `space_text.cc:162-192`. `from_string()` does **not** notify (`rna_text_api.cc:38-42`) — a gotcha |
| Console scrollback | yes (auto-follows end) | `bpy.ops.console.scrollback_append(text=..., type='OUTPUT'\|'INPUT'\|'INFO'\|'ERROR')` → appends a line and `ED_area_tag_redraw(area)` `[clone]` `console_ops.cc:1113-1134`, types `:1139-1145`; poll requires an active Console area (`ED_operator_console_active`, `:1155`). The Console space defines **no** `RGN_TYPE_UI` region (`space_console.cc` has no `RGN_TYPE_UI`), so you cannot put a panel there |
| Own window/area or a split-off Text Editor | n/a | BlenderGPT splits an area and retargets it to a Text Editor, then sets `spaces.active.text`: gd3kr/BlenderGPT `utilities.py:79-90`, used at `__init__.py:66-98` |
| GPU overlay in a viewport | you implement it | `SpaceView3D.draw_handler_add(callback, args, region_type, draw_type)` [`runtime`] + `blf` font drawing (`[docs]` `https://docs.blender.org/api/5.2/blf.html`) — draws over the viewport, not inside a panel; clipping/scrolling is yours |

### Manual pagination is the fallback for prose history

Since no per-panel scroll exists, a long prose transcript in a panel must be paginated by the addon itself
(keep an offset in a PropertyGroup, emit `Older ▲ / ▼ Newer` rows, plus a jump to newest on new output).
This is the only approach that keeps the whole transcript inside the panel.

---

## 4. Multi-line prompt entry

- **`layout.prop(data, "some_string_prop")`** renders a single-line text field (or `PASSWORD`/`FILE_PATH`
  subtypes). `bpy.props.StringProperty` has no height/line-count parameter; the widget is one line
  [`runtime`: `UILayout.prop` params are `text, text_ctr, icon, placeholder, expand, slider, toggle,
  icon_only, event, full_event, emboss, index, icon_value, invert_checkbox, align`]. So this cannot express
  a multi-line prompt.
- **`bl_property` + `invoke_search_popup` is not text entry.** `bl_property` only sets the operator's
  default property (`ot->prop`) drawn/expanded in the operator's properties dialog
  `[clone]` `source/blender/python/intern/bpy_operator_wrap.cc:44-77`; and `invoke_search_popup` exists to
  search the *items of an EnumProperty* `[clone]` `rna_wm_api.cc:1150-1156`, example
  `[clone]` `doc/python_api/examples/bpy.types.Operator.7.py:1-31`. Neither gives wrapped text.
- **`layout.textbox(...)` is the answer in 5.2.** RNA description [`runtime`]:
  "Exposes an RNA string property in the layout using a text-box widget with multi-line support.
  Text-box state will be stored in the current context region."
  Signature per `[clone]` `rna_ui_api.cc:1606-1618`: `textbox(data, property, initial_visible_lines=3,
  placeholder="", text_ctr="", translate=True)` — `initial_visible_lines` min 1; and
  `textbox_with_state(data, property, textbox_state, placeholder=…)` (`:1620-1630`) if you want to own the
  state. Implementation `Layout::textbox()` `[clone]` `interface_layout.cc:2804-2815` →
  `textbox_ensure_state(CTX_wm_region(C), "StructName.propname", initial_visible_lines)` — so it needs a
  real region, and the state (visible lines + scroll) is cached per region keyed by type+property name
  (`TextboxState { int visible_lines; int scroll; }` `[clone]` `DNA_screen_types.h:524-529`;
  `textbox_ensure_state` `buttons/interface_textbox.cc:326`).
  Bundled precedents for multi-line text in a *panel*:
  - `[installed]` `.../5.2/scripts/startup/bl_ui/properties_strip.py:316` — Text strip properties:
    `col.textbox_with_state(strip, "text", textbox_state=strip.textbox_state)`. This is a real,
    shipped 5.2.2 use of the multi-line widget inside a panel.
  - `[clone]` (5.3.0-alpha only) `scripts/startup/bl_ui/properties_output.py:258` — Render Output →
    Stamp → Note converted to `layout.textbox(rd, "stamp_note_text")`; the **installed 5.2.2 copy of
    that same panel still uses a single-line `layout.prop(rd, "stamp_note_text", text="")`**
    (`.../5.2/scripts/startup/bl_ui/properties_output.py:248-258`, read directly). So `textbox` is
    available in 5.2.2, but Blender's own conversion of ordinary text fields to it was still in
    progress at 5.2.
  - `[clone]` (5.3.0-alpha) `scripts/startup/bl_ui/space_filebrowser.py:799` — asset metadata editor
    uses `textbox(..., placeholder=...)`; the installed 5.2.2 file browser does not use it.
  It is a *editable* widget; I found no read-only variant in `rna_ui_api.cc` / `interface_layout.cc`.
- **Modal operator**: officially the recommended pattern for long-running interactive tools (gotchas RST,
  §1), and a modal operator can capture multi-line input (`event.value == 'PRESS'` + `event.unicode`),
  but you would be re-implementing caret/selection/wrapping/IME. Not justified while `textbox` exists.
- **Text Editor as the input box**: genuinely possible and the only surface that gives a full editor —
  user types into an addon-created `bpy.data.texts` datablock, the addon reads `text.as_string()`
  (`[clone]` `rna_text_api.cc:44-49`) when the send button is pressed. Trade-off: no Enter-to-send, and the
  editor must exist and be visible.

---

## 5. Host comparison (addons cannot register a new space type)

Available space/region pairs are limited to the `Panel.bl_space_type` / `Panel.bl_region_type` enums
[`runtime`]: 19 space types (`EMPTY`, `VIEW_3D`, `IMAGE_EDITOR`, `NODE_EDITOR`, `SEQUENCE_EDITOR`,
`CLIP_EDITOR`, `DOPESHEET_EDITOR`, `GRAPH_EDITOR`, `NLA_EDITOR`, `TEXT_EDITOR`, `CONSOLE`, `INFO`, `TOPBAR`,
`STATUSBAR`, `OUTLINER`, `PROPERTIES`, `FILE_BROWSER`, `SPREADSHEET`, `PREFERENCES`) × 17 region types
(`WINDOW`, `HEADER`, `CHANNELS`, `TEMPORARY`, `UI`, `TOOLS`, `TOOL_PROPS`, `ASSET_SHELF`,
`ASSET_SHELF_HEADER`, `PREVIEW`, `HUD`, `NAVIGATION_BAR`, `EXECUTE`, `FOOTER`, `TOOL_HEADER`, `XR`,
`SCRUBBING`) — and in practice only regions whose `ARegionType::draw` is a panels draw function host panels.

| Host | Default width | Always visible? | Scrolls? | Panel draw wired? | Precedent in bundled addons / startup |
| --- | --- | --- | --- | --- | --- |
| 3D Viewport sidebar `VIEW_3D` / `UI` | **280 px** (`UI_SIDEBAR_PANEL_WIDTH`, `[clone]` `UI_interface_c.hh:434`; set at `space_view3d.cc:1719`), user-resizable | No — hidden with `N`, and only if a 3D Viewport area exists | Yes (region View2D, `area.cc:4006-4026`) | yes (`art->draw = ED_region_panels_draw`, `space_view3d.cc:1726`) | `[installed]` `scripts/addons_core/viewport_vr_preview/gui.py:28-32` (`bl_category="VR"`, read from the 5.2.2 install); `[clone]` same addon files ship in 5.2.2 — `rigify/rig_ui_template.py:837-841` (`"Item"`), `pose_library/gui.py:43`, `io_scene_gltf2/.../gltf2_blender_ui.py:98-100` (`"glTF Variants"`) |
| Properties tabs `PROPERTIES` / `WINDOW` | fills the whole area (widest available) | No — only while that tab is active; competes with the object's real properties | Yes (same panels machinery, `space_buttons.cc:698-701`) | yes (`art->draw = ED_region_panels_draw`, `space_buttons.cc:1132`) | `[clone]` `rigify/ui.py:90-91`, `ui_translate/update_ui.py:118-119`, `hydra_storm/ui.py:11-12`, `io_anim_bvh/__init__.py:141-142`; alternatively `FILE_BROWSER`/`TOOL_PROPS` panels (`io_anim_bvh/__init__.py:141-142`, `:193-194`) |
| Text Editor sidebar `TEXT_EDITOR` / `UI` | **160 px** (`UI_COMPACT_PANEL_WIDTH`, `[clone]` `UI_interface_c.hh:433`; set at `space_text.cc:533`) | No — but it sits *next to* a scrollable multi-line buffer, which is the point | Yes (`ED_region_panels_init` at `space_text.cc:447`, draw `ED_region_panels` at `:456`) | yes | `[installed]` `.../5.2/scripts/startup/bl_ui/space_text.py:107-111` (`TEXT_PT_properties`, `bl_category="Text"`), `:140-144` (`TEXT_PT_find`) — read directly from the 5.2.2 install |
| Node Editor sidebar `NODE_EDITOR` / `UI` | 280 px (`space_node.cc:1869`) | No — needs a Node Editor area | Yes | yes | `[clone]` `scripts/addons_core/node_wrangler/interface.py:84-92` (`NODE_PT_nw_node_wrangler`, `bl_space_type='NODE_EDITOR'`, `bl_region_type="UI"`, `bl_category="Node Wrangler"`); the same addon ships in the 5.2.2 bundle |
| Others | e.g. `DOPESHEET_EDITOR`/`UI` (`pose_library/gui.py:124-125`), `SEQUENCER` sidebar at 1.3× (`space_sequencer.cc:1315`), `SPREADSHEET` (`space_spreadsheet.cc:842-848`) | same caveats | yes | yes | as listed |

Width facts that matter: the sidebar stops drawing panels when it is too narrow —
`min_draw_size = UI_PANEL_CATEGORY_MIN_WIDTH + PANEL_MIN_DRAW_WIDTH` (`[clone]` `area.cc:3940-3946`,
constants `UI_interface_c.hh:450`, `:454` where `PANEL_MIN_DRAW_WIDTH = 20`), and the width used by panels
is clamped by `panel_draw_width_from_max_width_get()` in `ED_region_panels_layout_ex`
(`area.cc:3569-3570`). Panels group into tabs via `Panel.bl_category`; `bl_order` sorts them; there is no
per-panel tab/accordion beyond `bl_options={'DEFAULT_CLOSED'}` [`runtime` `Panel` properties:
`bl_category, bl_context, bl_description, bl_icon, bl_idname, bl_label, bl_options, bl_order, bl_owner_id,
bl_parent_id, bl_region_type, bl_space_type, bl_ui_units_x, custom_data, is_popover, layout, text, use_pin`].

For a one-line "always somewhere visible" status (the closest thing to always-visible), use
`Area.header_text_set(text)` — the only other function on `Area` besides `tag_redraw`
[`runtime`: `Area.bl_rna.functions == [('tag_redraw','tag_redraw'), ('header_text_set','Set the header
status text')]`; RNA at `[clone]` `rna_screen.cc:475-486`]. bl_pkg uses the analogous workspace status
text for progress.

**Recommendation on placement:** `VIEW_3D`/`UI` as primary (precedent + resizable + scrollable + where a
Blender user looks for "N-panel" tools), `PROPERTIES` tab only if the panel needs real width, and
`TEXT_EDITOR`/`UI` + a `Text` datablock when full transcript history matters more than proximity to the
viewport.

---

## 6. Prior art: existing open-source chat/streaming panels

### `gd3kr/BlenderGPT` — "GPT-4 Blender Assistant" (MIT)

Real chat-in-a-panel implementation, and the closest thing to the target design:

- Panel: `GPT4_PT_Panel`, `bl_space_type='VIEW_3D'`, `bl_region_type='UI'`,
  `bl_category='GPT-4 Assistant'` — `__init__.py:100-107`.
- History: `column.box()` with one row per stored message (`row.label(text=f"User: …")`, per-message
  delete + "Show Code" operators) — `__init__.py:107-130`.
- Input: a **single-line** `column.prop(context.scene, "gpt4_chat_input", text="")`
  (`__init__.py:131-139`; property declared in `utilities.py:13-32` as a `StringProperty` on `Scene`),
  plus a chat-history `CollectionProperty`.
- Repaint: before the blocking request it calls
  `bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)` inside the execute method
  (`__init__.py:161-185`) — the hack the official docs discourage (§1).
- Streaming: the OpenAI response *is* streamed, but only printed to stdout
  (`utilities.py:64-78`, `print(completion_text, flush=True, end='\r')`); the panel receives the finished
  text only, and only after the blocking call returns.
- Code display: splits the area and retargets the new area to `TEXT_EDITOR`, then assigns
  `spaces.active.text` (`utilities.py:79-90`, used at `__init__.py:66-98`).

**Reusable:** host choice, the PropertyGroup message list, and the "dump generated code into a Text
datablock in a Text Editor" trick. **Not reusable:** the blocking execute path and the redraw hack — both
are what a timer + `tag_redraw()` design replaces.

### `ahujasid/blender-mcp` — "MCP for Blender" (MIT)

- Panel `BLENDERMCP_PT_Panel` in `VIEW_3D` / `UI` / `bl_category='MCP for Blender'`: connection status
  labels, checkboxes, API-key fields and buttons only — **no chat surface**; the conversation lives in the
  external MCP client.
- Useful mechanics for us, all in `addon.py`:
  - a single `bpy.app.timers` callback drains a `queue.Queue` on the main thread (`_drain_command_queue`,
    returning `0.05` to be called again) because "bpy.app.timers is not thread-safe, so registering a timer
    per command … could silently drop the callback";
  - it refuses to start a server under `blender -b`: "cannot start server in background mode … commands
    would never execute";
  - it repaints panels explicitly when state changes without user input:
    `_premium_tag_redraw()` loops windows/areas with `area.type in {"VIEW_3D", "PREFERENCES"}` and calls
    `area.tag_redraw()`.

### In-tree equivalent of "streaming text in a panel"

`[installed]` `bl_pkg` (§1): a timer drives progressive status text through
`region.tag_redraw()` + `region.tag_refresh_ui()` on a popup region while a network job runs. It is not a
chat, but it is the shipped, supported pattern for exactly the "text that grows while work is in flight"
problem, and it is inside the Blender distribution.

---

## Hard limits

- **No new space types from addons** (established) — and in practice a panel only appears in a region whose
  `ARegionType::draw` is `ED_region_panels_draw`/`_layout`. Console has no `UI` region at all.
- **A `Panel` cannot scroll.** `UILayout` has no scroll function or property [`runtime`]; `layout.box()` is
  a bordered sub-layout, not a viewport (`interface_layout.cc:5315`). Only the *region* (View2D), a
  `template_list`, a `textbox`, a Text Editor area, or the Console scrollback scroll.
- **`draw()` is re-executed and the panel's block is rebuilt on every repaint** (`area.cc:3358`,
  `wm_draw.cc:1004-1010`, `interface.cc:4052-4060`) — no incremental text append; per-frame cost is
  proportional to the widgets you emit, in Python.
- **Everything is main-thread, GUI-loop only.** Timers execute inside `wm_event_do_notifiers`
  (`wm_event_system.cc:596-603`) with only a window in context; nothing pumps under `blender -b`
  (established; corroborated by blender-mcp's explicit background check).
- **`bpy.ops.wm.redraw_timer` blocks the main thread** for `iterations` (`wm_operators.cc:3893-3950`), is
  GUI-only (`:3885-3891`), and is documented as an unaccepted-for-inclusion hack
  (`info_gotchas_internal_data_and_python_objects.rst:191-211`). Do not use it as the streaming pump.
- **`Region.tag_refresh_ui()` is popup-only** — its RNA wrapper errors on any non-`TEMPORARY` region
  (`rna_screen.cc:408-415`). Plain `tag_redraw()` has no such restriction.
- **Text is unstyled.** In 5.2.2 `UILayout` has `label` but no markdown/rich-text variant [`runtime`;
  full function list inspected]. Markdown label support exists only in the 5.3.0-alpha reference tree
  (`Layout::label_markdown`, `interface_layout.cc:3374`; `buttons/interface_label_markdown.cc`) — so it is
  not something 5.2.2 code can rely on. Line wrapping/wrapping width is Blender's, not yours.
- **`layout.textbox()` requires a real context region** (`CTX_wm_region(C)`, `interface_layout.cc:2804-2811`)
  and its scroll/visible-lines state is keyed per region by `StructName.propname`
  (`interface_layout.cc:2804-2815`, `DNA_screen_types.h:524-529`) — the same property shown in two panels
  shares one height/scroll state, and there is no built-in read-only mode.
- **Panel geometry is fixed by the region.** The sidebar's usable width is the region width minus
  panel-category margin (`area.cc:3569-3570`); it stops drawing when too narrow (`area.cc:3940-3946`);
  the default is 280 px (160 px for the Text Editor sidebar).
- **No always-visible surface exists.** Every panel host is a hidden-able area/region; the nearest
  always-on text channel is a status/header string (`Area.header_text_set` [`runtime`]).
- **Persistence caveat:** `Region` objects are validated by the workaround bl_pkg uses
  (`_region_exists` via `temp_override`, same file, ≈`:549`) — holding a
  `region` reference across events requires that check, and panes can be rebuilt at any time.

---

## What I could not determine

- **Measured cost/flicker.** I did not run a live 5.2.2 with a large streaming history, so "no flicker" is
  an inference from the redraw pipeline (full re-layout into the region's own buffer), not a measurement;
  likewise no fps/ms numbers for a Python `draw()` with N widgets.
- **`textbox` in every region type.** I verified the RNA/layout path and that it needs
  `CTX_wm_region(C)`, but did not test it in `FILE_BROWSER`/`TOOL_PROPS`, popups, HUD, or a
  `bl_region_type='WINDOW'` panels region on 5.2.2.
- **Read-only textbox.** No read-only flag exists in the RNA/wrapper that I read; whether a textbox bound to
  a non-editable StringProperty renders greyed and non-typable was not tested.
- **Whether a timer may call `bpy.ops.wm.redraw_timer`.** Its poll needs an active window and the timer
  context sets only a window (`wm_event_system.cc:572-573`); not tested, and irrelevant to the
  recommendation.
- **A reusable built-in multi-line text *input* operator.** I did not exhaustively scan `space_*.cc` for
  modal text-entry operators; the two entry points I confirmed are `textbox` and a Text Editor datablock.
- **Breadth of prior art.** I inspected two repositories (BlenderGPT, blender-mcp) plus the in-tree
  `bl_pkg`; I did not enumerate the extensions platform or GitHub generally, so "no other chat panel
  exists" is **not** asserted.
- **5.2.2 vs 5.3.0-alpha drift.** Every C citation is from the 5.3.0-alpha clone. I re-verified the
  Python-visible surface (available panel spaces/regions, `Region`/`Panel`/`UILayout`/`UIList`/`TextboxState`
  members, `Area` functions) directly on installed 5.2.2, but not each C behaviour (e.g. exact layout
  ordering internals) at 5.2.2.
- **Why `bl_pkg` calls `tag_refresh_ui()` from a timer.** It does so on a `TEMPORARY` region
  (`bl_extension_ops.py:241-250`), which is legal; whether the `_ui_refresh_apply()` path ever calls it on a
  non-popup region (which would emit a `BKE_report` error every step) was not traced exhaustively.
