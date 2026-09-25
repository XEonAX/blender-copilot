# Where chat history lives

Type: grilling
Status: open
Blocked by: none

## Question

Where does a conversation live between Blender sessions, and what is a "conversation" here?

Decide all of:

1. **Storage location.** JSON under Blender's user config / extension preferences directory (survives restart, per-user, does not pollute any `.blend`); inside the `.blend` as a Text datablock or custom property (travels with the file, but bloats it and leaks your prompts to whoever receives it); or in-memory only (lost on restart).
2. **Scoping.** Per-user, per-file (keyed by `.blend` path), or per-project directory? What happens when the user switches `.blend` files mid-conversation — resume the thread, start a new one, or refuse until they choose?
3. **What a conversation is.** One rolling stream per session, or named threads the user can start and return to?
4. **Retention.** How is history cleared, how is it capped, and does the user get an obvious way to delete it?

Weigh the fact that this is a *local developer tool* against the risk that a `.blend` containing a transcript is a distribution hazard.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
