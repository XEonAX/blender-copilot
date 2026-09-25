# The three tools' contracts

Type: grilling
Status: open
Blocked by: none

## Question

Pin the exact contract for the three tools in the slice, since the tool schema *is* the interface the model codes against.

For each tool — `run_blender_python`, `get_scene_info`, `get_rna_info` — decide:

1. **Input schema.** Exact parameters and their defaults.
2. **Output shape.** What comes back, in what structure, and capped how.
3. **Failure contract.** What the model sees when the call fails.

Specifically:

- **`run_blender_python`**: does the executed code get a *fresh* namespace per call or a *persistent* one across calls? (Persistent lets the model build state up, but makes failures harder to reason about and leaks half-finished objects between calls.) How much of stdout/stderr/traceback is returned — full traceback, or summarised? Where is the truncation cap, and does the model get told it was truncated?
- **`get_scene_info`**: what scope — whole scene, active object, selection, or a named subset — and what does it return for a scene with thousands of objects? What are the include flags and the caps?
- **`get_rna_info`**: what can it actually answer without dumping the API — property lookup on a type, operator signature and poll requirements, enum values, "which operators exist"? It exists to stop the model hallucinating `bpy` APIs; define what it must answer to achieve that.
- Whether a failed call returns control to the model, and whether anything auto-retries (the retry policy itself belongs to *The agent loop's control flow and failure policy*).

Depends on the execution and undo facts in *What exactly happens when we exec model code, and what does undo cover?* — read its answer first.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
