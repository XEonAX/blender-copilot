# The transport, built: two runs that measured it

Ticket: `.scratch/blender-copilot/issues/11-http-transport-thread-or-subprocess.md`

Ticket 11 *designed* the transport and ticket 16 measured the provider's wire
contracts. This note is the first evidence that the two meet: the subprocess,
the pipe, the SSE accumulation and the cancel path, all running.

Both runs use the installed **Blender 5.2.2** and its bundled Python
**3.13.13**. Both print a verdict token, because a caller cannot gate a Blender
invocation on its exit code. Neither ever prints the API key.

## 1. The transport, headless

    set -a; . ./.env; set +a
    python3 tools/bounded_run.py 180 -- \
        /Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13 \
        tools/transport_smoke.py

```
SMOKE | endpoint https://api.deepseek.com, model deepseek-flash, key present (never printed)
SMOKE | turn 1 sent after 0.001s (no wait for ready)
SMOKE | turn 1 events: 32 (delta, done, reasoning)
SMOKE | turn 1 first delta after 1.004s
SMOKE | turn 1 reasoning characters seen: 111
SMOKE | turn 1 reply: 'pong'
SMOKE | turn 2 stopped after 0.02s (the child acknowledged)
SMOKE | turn 2 kept 42 characters, marked [stopped]
SMOKE OK
BOUNDED | finished on its own in 5.5s | rc=0
```

Read off it:

- **`ready` is never awaited.** The `send` command was on the wire 0.001 s after
  `Popen`, and the reply still arrived - the queue-based start works as designed.
- **One child served both turns.** The second `send` did not spawn a process, and
  the child was still alive at the end.
- **Cancel is the fast path, not the kill.** 0.02 s from `{"cmd":"cancel"}` to
  the child's own `stopped` event - the `raw.shutdown()` unblock ticket 03
  measured, reached through the process boundary. `proc.kill()` (2 s grace) never
  fired, which is the point of having it as a backstop rather than the mechanism.
- **Thinking is visible as a count.** 111 reasoning characters crossed the pipe
  as counts, never as text, so the panel can say `thinking…` without pretending
  the thought is the answer.
- **No key in the log.** Asserted by the harness against the whole transcript.

## 2. The panel, in a real GUI session

    set -a; . ./.env; set +a
    python3 tools/bounded_run.py 150 -- \
        /Applications/Blender.app/Contents/MacOS/Blender \
        --python tools/panel_round_trip.py

```
TURN | global undo on: True
TURN | send() -> {'FINISHED'}
TURN | turn opened, streaming=True, status='waiting…'
TURN | transcript kinds: ['user', 'assistant']
TURN | reply (4 chars): 'pong'
TURN | repaints: 2 calls, 2 tagged a region
TURN | RESULT one real turn, 4 characters, streamed and repainted
SMOKE OK
BOUNDED | finished on its own in 2.3s | rc=0
```

This run exists because `bpy.app.timers` pump **only** from the GUI event loop,
so the one seam between the worker and the panel cannot be reached under
`blender -b`. It went through the panel's own `Send` operator against the user's
live configuration, and the harness *counted* the redraws rather than looking at
them: 2 calls, both of which tagged a real region. Blender quit itself, so the
deadline was a backstop.

**No screenshot was taken, and no instance has seen the panel render.** Whether
this looks right is still the human's call, and it is the same outstanding visual
pass `docs/ratification.md` already lists for ticket 08.

## What these runs do not cover

- **Stop from the button.** The headless run cancels at the transport level; the
  GUI run does not press Stop, so the operator path itself is unexercised.
- **A second turn in one session** (history being resent). The headless run sends
  two turns but the second is cancelled before the model sees the first reply as
  history.
- **Anything after the first token: tools, the store, the undo push.** Not built.
