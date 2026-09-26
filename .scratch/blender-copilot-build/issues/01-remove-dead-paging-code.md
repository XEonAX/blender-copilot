# 01: Remove the dead paging code

**What to build:** nothing a user can see — this is the prefactor, done first so the
rest of the build does not read two contradictory designs. The panel used to page
its transcript; that was rejected and the panel now renders the whole conversation
and lets the sidebar region scroll. The paging machinery is still present, still
registered, and still covered by unit checks that assert behaviour the panel no
longer has. Delete it, checks included.

**Blocked by:** None (can start immediately)

**Status:** open
**Triage:** ready-for-agent

- [ ] The paging operators are gone and nothing registers them.
- [ ] The page-packing helper, the page-size constant and the session's page cursor are gone.
- [ ] The unit checks that covered paging are removed with them, so no check asserts behaviour the panel no longer has.
- [ ] The whole transcript still renders and the sidebar region still scrolls.
- [ ] The panel draw check passes and prints its success token.
- [ ] Nothing else in the extension changed behaviour — this is a deletion, not a redesign.
