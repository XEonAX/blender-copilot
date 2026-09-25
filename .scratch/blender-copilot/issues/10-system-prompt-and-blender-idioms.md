# What the prompt teaches the model about Blender

Type: grilling
Status: open
Blocked by: 06

## Question

What guidance stops the model making the mistakes an LLM reliably makes in Blender — and where does each piece live?

Decide the content and its placement across three surfaces: the system prompt, the per-tool descriptions (which are prompt surface too), and a skill asset.

Candidate content, to accept, reject, or extend:

- **API idioms that matter**: prefer named socket access (`bsdf.inputs["Base Color"]`) over hard-coded indices, because socket order changes between versions; version-dependent API churn in 5.x; collection vs `bpy.data` link semantics; `bpy.context.temp_override` for operators that fail their poll; what to do instead of an operator whose poll keeps failing.
- **Undo discipline as a *tool requirement***: one meaningful change per tool call, so each call is an individually revertable step. This is the reason the model must not bundle six edits into one script — it is not stylistic advice, it is what makes the recovery mechanism work.
- **Units, scale, and axis conventions**, and how to ask about them rather than assume.
- **Inspect before mutating**: when the model should call `get_scene_info` / `get_rna_info` first rather than guessing.
- **What the live summary contains**, and that it is authoritative for present-tense UI state.
- **Tone/behaviour**: state the resolved target before mutating; never claim a change succeeded without reading back the result.

Deliverable: the drafted guidance, split by where it lives, with a note on what was deliberately left to the tool schemas instead of the prompt.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
