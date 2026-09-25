# Where the API key lives, and how the user sets it

Type: grilling
Status: resolved
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

**RATIFIED 2026-09-25 by the project owner — accepted as written**, including §1's
plaintext-in-`userpref.blend` call and §4's empty model field. The record is
[docs/ratification.md](../../../docs/ratification.md). **Note added the same day:**
§4's stated reason for shipping no default model — that the session could not
verify a model string exists — is now discharged. The backend is DeepSeek
(`https://api.deepseek.com`, OpenAI format) and its live docs give the model
names `deepseek-flash` and `deepseek-v4-pro`, with the older `deepseek-v4-flash`
still accepted but *remapped* to a retired model. The decision stands as ratified
(empty until filled in) and pinning `deepseek-flash` is now a one-line change
with evidence behind it.

**Measured 2026-09-26** by *The first live send against DeepSeek*: the legacy name
`deepseek-v4-flash` is **accepted with HTTP 200** and answered, and every response
echoed `"model": "deepseek-flash"`. The retired name does not error — it silently
serves something else, and the only tell is a field the caller must think to read.
That is the argument for this section's empty default, now measured rather than
quoted, and it also says what to add: whatever the user types should be validated
against the provider's live model list at Send time.

### Verified this session (installed 5.2.2, `--background`, no GUI launched)

- No OS-keychain binding exists in Blender's Python: `import keyring`, `secretstorage`,
  `win32cred` each raise `ModuleNotFoundError`.
- `StringProperty(subtype="PASSWORD")` registers on an `AddonPreferences`, and masking is
  real but **display-layer only**. `interface_widgets.cc:3061-3065,3269` plus
  `interface_handlers.cc:3180` (`button_text_password_hide`) replace the drawn glyphs with
  bullets; the tooltip refuses to show it (`interface_region_tooltip.cc:1140-1141`);
  copy-as-python-string returns early (`interface_handlers.cc:1131`). Those are **5.3.0-alpha
  clone** paths — the installed app ships no C source — so treat the masking detail as
  inherited, not as 5.2.2-verified. Python still reads the raw value: `prefs.api_key` printed
  the secret verbatim.
- **Preferences are plaintext on disk, and no preference field can avoid it.** Installed the
  real extension into an isolated `BLENDER_USER_EXTENSIONS` dir with a PASSWORD `api_key`
  property, enabled it, set `sk-REAL-EXT-PROBE-99887766`, `save_userpref()` → `strings
  userpref.blend` shows the secret (line 66) and the model string (line 70); it reads back
  verbatim after a restart. Mode `-rw-r--r--`.
- `options={"SKIP_SAVE"}` **does not help**: `is_skip_save` reports `True`, the secret still
  appears in `userpref.blend` and survives the restart. "Masked field that never persists"
  does not exist on this platform.
- No scrub hook exists: `bpy.app.handlers` has **no** `preferences_save_pre` (only
  `load_factory_preferences_post`, `load_factory_startup_post`, `exit_pre`).
  `AddonPreferences` itself exposes only `bl_idname` — no secret-storage hook.
- Blender treats its own secret the same way: `UserExtensionRepo.access_token` is
  `subtype='PASSWORD'`, `is_skip_save=False`, drawn at `space_userpref.py:2321`, persisted in
  `userpref.blend`.
- Env vars are visible to Blender's Python, but **launchd does not inherit the shell's env**:
  with `OPENAI_API_KEY` exported in the shell, `launchctl getenv OPENAI_API_KEY` is empty. A
  Finder/Dock launch therefore cannot see a key put in `.zshrc` — env-only means "always
  launch Blender from a terminal" on macOS.
- Parking a settings `PropertyGroup` on `Scene` to dodge preferences puts the value **inside
  every saved `.blend`**: with `compress=False` a scene custom property is plainly in the file
  and reloads. (The same save without `compress=False` produced a file where `strings` finds
  nothing — zstd, not encryption.)
- `bpy.ops.screen.userpref_show` exists, and the bundled keymap binds it to `Ctrl+,`
  (`presets/keyconfig/keymap_data/blender_default.py:885`).
- Carried in from *Where chat history lives*: `extension_path_user(__package__, ...)` is the
  per-extension user dir.

### 1. Winner — addon preferences, PASSWORD field, env var as an override that wins. Plaintext is accepted out loud.

Resolution order per send: non-empty `OPENAI_API_KEY` in `os.environ` → else
`BlenderCopilotPreferences.api_key` → else "not set". The field's `description` reads
`Stored unencrypted in Blender's userpref.blend.`, and the panel always prints the source it
used: `key: env …A1B2` / `key: prefs …A1B2` / `key: not set`. A user who does not want the
key on disk exports the env var and leaves the field empty — then nothing is written.

**The trade-off, stated plainly:** this writes a bearer token, in cleartext, into
`~/Library/Application Support/Blender/5.2/config/userpref.blend`, mode `0644`, with no
encryption and no way to exclude the property. It is exposed to anything that reads that file:
another local account, a config/Time-Machine backup, or a pasted-in bug report. That is the
price of the in-panel field, and the only real gain foregone against a 0600 file is that
permission bit.

Rejected:
- **Env-var-only.** Safest, and it fails the product premise on macOS — the verified launchd
  fact means the user must launch Blender from a terminal forever, and a first run that
  cannot be fixed from inside the panel is exactly the failure the panel exists to avoid.
- **`keyring` wheel.** Not bundled; pulls backend deps; the manifest wheel path has no
  collision handling, so it becomes a standing conflict risk for one private user. Payoff is
  only versus another local account on a single-user machine.
- **Our own 0600 secrets file under `extension_path_user(...)`.** Better permission bit and
  deletable, but the masked widget must still bind to an RNA property, and any RNA-bound
  field lands in `userpref.blend` (SKIP_SAVE verified useless). That is two copies of the key
  instead of one. Rejected.
- **A `PropertyGroup` parked on `Scene`/`WindowManager`.** Verified to write the value into
  every saved `.blend` — the distribution leak *Where chat history lives* already rejected.

### 2. Set, see, rotate

- **Set:** `layout.prop(settings, "api_key")` in the panel's settings block; the field is
  masked as you type. The same field is reachable via Edit ▸ Preferences ▸ Add-ons (`Ctrl+,`,
  verified binding).
- **See:** never revealed. Blender cannot un-mask a PASSWORD field, and mirroring the value
  into a second plain `StringProperty` would add a second RNA path that also persists. Instead
  show a fingerprint label — source plus last four, e.g. `prefs …A1B2`; empty renders as
  `not set`. Enough to tell two keys apart while rotating, not enough to be a leak.
- **Rotate:** overwrite in place, plus a `Clear` button that sets it to `""`. No confirm
  dialog: nothing destructive is at stake (the transcript is separate) and a bad key is fixed
  by retyping. Rotating an env-sourced key requires a relaunch; the `env` label is what makes
  that discoverable.
- The raw key never enters the transcript, an error string, or a log. Redact
  `sk-[A-Za-z0-9_-]+` → `sk-…` at the single error-formatting boundary.

### 3. Missing key (first run) and invalid key (mid-turn)

- **Missing:** the panel draws a `Not configured` block instead of the transcript — key field
  first, then base URL and model — and `Send` returns `{"CANCELLED"}` with a panel-visible
  line `Set an API key to send.` `self.report` alone is not the surface; the panel is. No
  request is attempted, so there is no round trip to fail.
- **Invalid (401/403):** transport raises a distinct `AuthError`; the turn ends. Already-streamed
  partial assistant text stays in the transcript (it is real model output and discarding it
  would be a lie), then one line is appended: `Auth failed (401). Key rejected. Update it in
  settings.` **No automatic retry** — an unchanged key can only fail again — and no history
  clear. If the key came from the environment the line says so, because editing the field would
  otherwise look broken.
- **`404 model_not_found`** is its own class, naming the model string and pointing at the Model
  field. This is load-bearing for §4.

### 4. Sibling settings — same place, and the model default is *empty*

- `base_url`: same prefs block as the key; default `https://api.openai.com/v1`; env override
  `OPENAI_BASE_URL`.
- `model_name`: same prefs block; **default `""` = unset, and `Send` refuses while it is
  empty** with a panel line naming the field. Rationale: model names churn, and this session
  could not verify a single current one — `/v1/models` answers 401 without a key — so any
  shipped default is a guess that fails on the first real send. An explicit required field is
  the honest version of the same friction, and the §3 `404` line makes a wrong value cheap to
  fix. Env override `OPENAI_MODEL`.
- Both are non-secret, so prefs are safe for them regardless of §1.

### 5. Scope — addon preferences, not a settings file, not per-conversation

- Per-user and global, in `AddonPreferences`, because `layout.prop` needs a registered RNA
  owner and preferences are the only such owner that is neither saved into a `.blend` (Scene,
  verified above) nor a wheel concern.
- The **effective** `model` and `base_url` are *recorded* in the conversation file's header for
  display only — two non-message fields added to ticket 04's format. A resumed thread shows
  what it actually ran under instead of silently adopting today's setting.
- Mid-conversation switching stays deferred to the map's *whether chaining models or base URLs
  mid-conversation is safe* frog; nothing here presumes it works.
- Rejected: an extension-level `settings.json` under `extension_path_user(...)` — it buys 0600
  and separability but needs hand-rolled load/save *plus* an RNA owner for the widget, and the
  only RNA owners available leak the value into a file. Per-conversation overrides — rejected:
  a resumed thread would run under different settings than it was built with.

### What a human must ratify

Two deliberate, contestable calls:

1. That the API key is written **unencrypted into `userpref.blend`** by default, with the env
   var as the opt-out — rather than env-only (which on macOS means never launching Blender from
   the Dock) or a `keyring` wheel.
2. That `Model` ships with **no default** and `Send` refuses until it is filled in, rather than
   pinning a model string this session could not verify exists.

Overruling either changes only §1 or §4 respectively; §2, §3 and §5 stand as written.

## Comments
