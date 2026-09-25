# How a conversation is laid out and controlled

Type: prototype
Status: open
Blocked by: none

## Question

With a panel that renders and repaints, decide how a real conversation reads and how the user controls it.

Decide, by building **two or three contrasting concrete layouts** and reacting to them rather than arguing abstractly:

1. How user turns and assistant turns are visually distinguished.
2. **How model-authored code and its output are shown.** Inline code block, collapsible, or a separate area? The user has to be able to read the code the model is about to run, even without an approval gate. **Constraint, verified:** `UILayout` has no rich text in 5.2.2 (`label` accepts only text/icon — no markup), so there is no in-panel code styling and no monospace control. Decide whether code belongs in the panel at all or in a companion Text Editor area bound to an addon-owned `Text` datablock.
3. How a running tool call is indicated, and what a finished one looks like afterwards.
4. How errors surface (a failed `bpy` call, a failed API call) without wrecking the transcript.
5. **The revert affordance.** Auto-run with no gate means the user's control is *after* the fact — so how do they see that the last run is undoable, and how do they walk it back? Note that Ctrl+Z is global and the agent has been pushing its own undo steps; decide whether the panel exposes its own revert control or simply teaches the user that Ctrl+Z walks the agent's work back.
6. Where Stop lives, and what it does to an in-flight tool call versus an in-flight stream.

Deliverable: the chosen layout and why the rejected alternatives lost.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
