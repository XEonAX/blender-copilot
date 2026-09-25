# blender.anx.copilot

A Copilot-style chat panel that lives **inside Blender**. Development happens
here; `/Users/user/Projects/blender` is a read-only reference clone.

**Status: prototype.** The panel renders a canned conversation and a fake
streaming reply. No model is called, no tool is executed, nothing is persisted.
Its only job is to prove the panel is a viable chat surface before an agent loop
is built on it. The route from here is charted in
[`.scratch/blender-copilot/map.md`](.scratch/blender-copilot/map.md) — eighteen
tickets: **fourteen resolved**, two that need a human, two open. Every decision in
there was made by an agent with nobody in the loop, and then **ruled on by the
project owner on 2026-09-25** — fifteen accepted as written, one rejected. The
rejection was the approval gate, so model-authored code auto-runs unscoped; the
reasoning and its consequences are in
[`docs/ratification.md`](docs/ratification.md).

## Layout

```
blender_copilot/          the Blender extension (this is the package)
  blender_manifest.toml   extension metadata; declares the network permission
  __init__.py             register() / unregister()
  panel.py                the panel, its operators, and its preferences
  conversation.py         conversation state + the fake reply producer
  stream.py               the repaint pump
tests/test_conversation.py   runs on plain CPython, no Blender needed
tools/undo_probe.py       one-off empirical undo check (run manually, GUI)
dist/                     built packages (gitignored)
```

## Target

The **installed** Blender at `/Applications/Blender.app` — **5.2.2**, Python
3.13.13. `blender_version_min` is pinned to `5.2.0` in the manifest.

## Dev loop

### Fast loop: symlinked extension directory

Edit files in place; Blender picks them up on restart. No zip, no reinstall.

```sh
EXT="$HOME/Library/Application Support/Blender/5.2/extensions/user_default"
mkdir -p "$EXT"
ln -sfn "$PWD/blender_copilot" "$EXT/blender_copilot"
```

Then enable **Blender Copilot** once in *Preferences → Add-ons*. If
`user_default` is missing from *Preferences → Get Extensions → Repositories*,
add a local repository first.

### Real loop: build and install a package

```sh
mkdir -p dist                      # the builder does NOT create this itself
/Applications/Blender.app/Contents/MacOS/Blender -c extension build \
    --source-dir ./blender_copilot --output-dir ./dist

/Applications/Blender.app/Contents/MacOS/Blender -c extension install-file \
    -r user_default -e ./dist/blender_copilot-0.0.1.zip
```

`-e` enables it on install. Add `validate` before `build` to check the manifest.

### See it

Open the 3D Viewport, press **N** for the sidebar, and pick the **Copilot** tab.
Type into the box, press **Send**, and watch the reply stream in.

### Compare the conversation layouts

The panel renders one demo conversation in three candidate layouts, switchable
live at the bottom of the panel (**Layout (prototype)**):

- **Role boxes** — user turn as a labelled block, assistant turn as one bordered box.
- **Flat log** — prefixed plain text, no borders.
- **External transcript** — panel keeps only controls; transcript and code live in
  the `Copilot Transcript` / `Copilot Code` Text datablocks.

The decision and the rejected alternatives are on
[`.scratch/blender-copilot/issues/08-panel-conversation-ux.md`](.scratch/blender-copilot/issues/08-panel-conversation-ux.md).

```sh
# run every layout's draw body with a stub UILayout (no GUI needed)
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
    --python tools/panel_draw_smoke.py
```

## Verified / not verified

Verified on 2026-09-25 against Blender 5.2.2:

- manifest parses (`extension validate`)
- all four classes register and unregister without error
- 42 unit checks on the conversation and wrapping logic pass on plain CPython
- the package builds, installs, and enables as `bl_ext.user_default.blender_copilot`
- preferences bind correctly, so the textbox has an RNA string to attach to
- the symlinked extension directory is discovered and enables
- `tools/capability_probe.py` runs headlessly and reproduces its own numbers
  (77 attempts: 49 denied, 2 escaped, 11 blocked, 15 allowed), so the capability
  boundary in ticket 13 is a measurement rather than a claim

**Not verified, because it needs a human looking at a GUI:** that the panel
draws as intended, that the textbox renders and writes back, that the streamed
repaint is smooth rather than janky, that `Show code` splits an area correctly,
and the undo behaviour in `tools/undo_probe.py`.

## Two things that shape the design

- **A panel cannot scroll, and has no rich text.** `UILayout` offers neither, so
  the transcript truncates older lines and code renders as plain text. Long
  history and code display may need a companion Text Editor surface.
- **The repaint must be earned.** The timer only tags a redraw when the text
  actually changed, and unregisters itself when the stream ends — an idle panel
  costs nothing.
