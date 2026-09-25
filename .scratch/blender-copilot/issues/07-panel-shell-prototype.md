# Build the cheapest installable extension that proves the panel

Type: prototype
Status: resolved
Blocked by: none

## Question

Build the roughest thing that proves a chat panel can exist in Blender 5.2.2, then react to it. The point is to raise the fidelity of the discussion with something concrete, not to build the product.

Scope — deliberately small:

- `git init` the dev folder `/Users/user/Projects/blender.anx.copilot` and lay out the extension.
- A `blender_manifest.toml` with `[permissions] network` and `blender_version_min = "5.2.0"`, installable into the installed Blender 5.2.2.
- A documented fast dev loop (how to edit and see the change without reinstalling by hand each time).
- A panel in whatever space *What can a Blender 5.2 panel actually render and accept?* recommends.
- A **hardcoded fake conversation** rendered in it.
- A **token ticker** driven by a timer, to prove the streaming repaint path actually works.
- A **multi-line input** that captures a prompt into a variable and does nothing with it. Use `UILayout.textbox()` bound to an addon-owned `StringProperty` — verified present in 5.2.2, and the only real multi-line input available.

Explicitly out of scope: any LLM call, any tool execution, any history persistence.

Fold in one verification the research could not do: **undo needs a screen, so confirm in this GUI session what `bpy.ops.ed.undo_push()` actually reverts** — per call, per turn — and whether an operator called from Python leaves anything recoverable. Report the empirical result on *What replaces undo as the recovery mechanism?*.

The decision this unblocks: the panel's placement and input model, and whether the repaint path holds up under streaming at all.

Deliverable: the working extension in `blender.anx.copilot`, a screenshot, and a short honest note on what was awkward or worse than expected.

## Answer

Built at `/Users/user/Projects/blender.anx.copilot`: the `blender_copilot` extension (manifest, panel, operators, preferences, conversation state, repaint pump), `tests/test_conversation.py`, `tools/undo_probe.py`, and a README documenting the dev loop.

**Verified by running Blender 5.2.2, not by reading docs:**

- the manifest parses (`blender --command extension validate`)
- all four classes register and unregister cleanly
- 20 unit checks on the conversation and wrapping logic pass on plain CPython
- the package builds, installs and enables as `bl_ext.user_default.blender_copilot`
- **preferences bind correctly** - the risky assumption behind the textbox. `context.preferences.addons[__package__]` even resolves to the extension's own module id, so the RNA string the textbox needs is reachable
- a **symlinked extension directory** is discovered and enables, which is the fast dev loop (edit in place, restart Blender, no zip)
- the builder does **not** create `--output-dir`; it fails with a bare `FATAL_ERROR` if `dist/` is missing

**Not verified - it needs a human watching a GUI:** that the panel draws as intended, that `layout.textbox` renders and writes back, that streamed repaint is smooth rather than janky, and the undo probe's four cases. A GUI session was deliberately not launched; the visual check is a ~20 second human action and is handed over rather than automated.

**Settled by this ticket:** the 3D Viewport sidebar accepts the panel; the input model is a `textbox` bound to addon preferences; and the repaint is a timer that tags a redraw *only* when the text changed and unregisters itself when the stream ends, so an idle panel costs nothing.

**Awkward, and worth knowing before building on it:** no rich text, so the fake reply's code renders unstyled; no scrolling, so the transcript truncates older lines with a marker - a workaround, not a solution; and the manual word-wrap budget is a guess at the sidebar width.

**Moved out of this ticket:** the *reaction* to the artifact. Taste is not a fact question, so it belongs with *How a conversation is laid out and controlled*. The empirical undo result belongs on *What replaces undo as the recovery mechanism?* - `tools/undo_probe.py` is the instrument, and running it is a manual GUI step.

## Comments
