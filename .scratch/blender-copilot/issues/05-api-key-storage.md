# Where the API key lives, and how the user sets it

Type: grilling
Status: open
Blocked by: none

## Question

Where does the LLM API key live, and what is the first-run experience of configuring it?

The three candidates, with the fact that decides the trade-off:

- **Environment variable** (or a gitignored file). Nothing secret is written anywhere Blender owns. Slightly worse UX: it must be set before Blender launches.
- **Addon preference field.** Best UX, but Blender persists addon preferences into its user preference file — a `.blend`, **not encrypted** — so the key ends up plaintext on disk.
- **`keyring` wheel** for the OS keychain. Most correct; adds a wheel dependency to the extension (Blender ships no wheel-resolution conflict handling, so a wheel that collides with another extension's is a real hazard).

Decide:

1. Which storage wins, and the plaintext-or-not trade-off stated out loud rather than glossed.
2. How the user sets, sees (masked?), and rotates the key.
3. What happens when the key is missing (first run) or invalid (auth failure mid-turn) — the exact error surface in the panel.
4. Where the sibling settings live: base URL and model name. Same place as the key, or separate, and what their defaults are.
5. Whether these are addon preferences, an extension-level settings file, or per-conversation overrides.

## Answer

<!-- recorded on resolution; not written at chart time -->

## Comments
