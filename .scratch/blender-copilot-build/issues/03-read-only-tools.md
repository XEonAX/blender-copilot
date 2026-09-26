# 03: The read-only tools

**What to build:** the other two thirds of the tool surface, so the model can look
before it acts instead of guessing. Ask "what's in my scene?" and get a real answer
without the panel dumping the scene into the model's context; ask "what does this
operator take?" and get it from the live build rather than from anything
hand-written.

**Blocked by:** 02 (One tool call, end to end)

**Status:** open
**Triage:** ready-for-agent

- [ ] "What's in my scene?" is answered from a bounded summary — counts, plus a capped list with a truncation flag — never a full dump.
- [ ] Asking for more than the cap returns the cap and says it was truncated, rather than silently shrinking.
- [ ] "What arguments does this take?" is answered by exact-name lookup against the live build; an unknown name reports not-found rather than inventing an answer.
- [ ] The long-tail guidance kind exists, so idiom questions do not require a fourth tool.
- [ ] Both tools respect the shared result-size cap, and a truncated result says so.
- [ ] The prompt no longer needs to shy away from naming the tools it now actually has.

**Context:** *The three tools' contracts* fixes the envelope and the caps;
*What the prompt teaches the model about Blender* fixes where guidance lives and
why runtime introspection beats a hand-written API list. Note that the
`{capability}` line from the latter is **moot** — the capability restriction was
rejected — so the prompt must not describe a restriction the runtime does not
enforce.
