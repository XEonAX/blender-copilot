#!/usr/bin/env bash
#
# Implement ONE build ticket with a single non-interactive `pi` process.
#
#   tools/build-ticket.sh --dry-run 01
#   tools/build-ticket.sh 01
#
# The serial sibling of `wayfinder-wave.sh`. That one runs *decision* tickets in
# parallel and expects prose; this one runs *one build* ticket at a time and
# expects working code, so it is deliberately not a wave.
#
# It refuses to start a ticket that is not `open`, or whose blockers are not all
# `resolved`. The orchestrator walks the dependency graph by hand and that is
# exactly where it can slip, so the check lives here rather than in its head.
#
# It does not commit and it does not flip the status on the caller's behalf. The
# orchestrator verifies the ticket independently before either happens - an
# executor marking its own homework is not a check.
#
# The full working protocol lives in AGENTS.md, which pi loads as a context file
# in every mode, including print mode and regardless of project trust.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISSUES="$REPO/.scratch/blender-copilot-build/issues"
LOGS="$REPO/logs"
SKILLS="${PI_SKILLS_DIR:-$HOME/.agents/skills}"
BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"
BLENDER_PY="/Applications/Blender.app/Contents/Resources/5.2/python/bin/python3.13"

DRY_RUN=0
TARGET=""

for arg in "$@"; do
  case "$arg" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "usage: $(basename "$0") [--dry-run] <ticket>"
      echo "  <ticket> is a number (01), a prefix, or a filename"
      echo "  env: PI_SKILLS_DIR overrides ~/.agents/skills"
      exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *)  TARGET="$arg" ;;
  esac
done

[ -n "$TARGET" ] || { echo "usage: $(basename "$0") [--dry-run] <ticket>" >&2; exit 2; }
command -v pi >/dev/null 2>&1 || { echo "error: pi not on PATH" >&2; exit 1; }
[ -d "$ISSUES" ] || { echo "error: no tickets at $ISSUES" >&2; exit 1; }

FILE=""
if [ -f "$ISSUES/$TARGET" ]; then
  FILE="$TARGET"
else
  for candidate in "$ISSUES"/"$TARGET"*.md; do
    [ -f "$candidate" ] && FILE="$(basename "$candidate")" && break
  done
fi
[ -n "$FILE" ] || { echo "!! no ticket matches '$TARGET'" >&2; exit 1; }

ID="${FILE%%-*}"
status_of () { grep -m1 '^\*\*Status:\*\*' "$1" | sed -E 's/^\*\*Status:\*\*[[:space:]]*//'; }

STATUS="$(status_of "$ISSUES/$FILE")"
BLOCKERS="$(grep -m1 '^\*\*Blocked by:\*\*' "$ISSUES/$FILE" | sed -E 's/^\*\*Blocked by:\*\*[[:space:]]*//')"

echo "ticket:   $FILE"
echo "status:   ${STATUS:-<missing>}"
echo "blocked:  ${BLOCKERS:-<missing>}"
echo

if [ "$STATUS" != "open" ]; then
  case "$STATUS" in
    claimed)
      echo "!! already claimed. Read logs/build-$ID.log and check whether anything is" >&2
      echo "   still running. If it is a stale claim, reset Status to 'open' yourself." >&2 ;;
    resolved)
      echo "!! already resolved, so a verified ticket has been re-queued. If the code" >&2
      echo "   is wrong, file a new ticket that names this one. If you are the" >&2
      echo "   orchestrator correcting a failed verification, reset Status to 'open'" >&2
      echo "   first and record the failure under '## Verification failed'." >&2 ;;
    human-required)
      echo "!! parked for a human. Not yours to start - see the DO NOT CLAIM banner." >&2 ;;
    *)
      echo "!! status '${STATUS:-<missing>}' is not one of open/claimed/resolved/human-required." >&2 ;;
  esac
  exit 1
fi

blocked=""
for dep in $(printf '%s' "$BLOCKERS" | grep -oE '(^|,)[[:space:]]*[0-9]{2}' | grep -oE '[0-9]{2}'); do
  depfile=""
  for candidate in "$ISSUES"/"$dep"-*.md; do
    [ -f "$candidate" ] && depfile="$candidate" && break
  done
  if [ -z "$depfile" ]; then
    blocked="$blocked $dep(no-such-ticket)"
    continue
  fi
  depstatus="$(status_of "$depfile")"
  [ "$depstatus" = "resolved" ] || blocked="$blocked $dep(${depstatus:-<missing>})"
done
if [ -n "$blocked" ]; then
  echo "!! not on the frontier - blockers unresolved:$blocked" >&2
  exit 1
fi
echo "frontier: yes - every blocker resolved"
echo

build_prompt () {
  cat <<EOF
Implement exactly one build ticket. You are the implementer; a separate
orchestrator verifies you afterwards, so do not mark anything that a command
did not show.

Repo root:    $REPO
Ticket:       .scratch/blender-copilot-build/issues/$FILE

Do this now, in order:

1. BEFORE any other work, edit the ticket file and set Status to "claimed".
   That edit is the claim and nothing else may precede it.

2. Read the ticket in full. Then read every decision ticket it names. It names
   them by title, so find them by searching .scratch/blender-copilot/issues/
   for the title. Those decisions are ratified by the project owner: transcribe
   them, do not re-decide them. If one turns out to be wrong, say so in your
   answer rather than silently departing from it.

3. Read AGENTS.md and obey it. It carries the house facts - what Blender's
   Python bundles, how undo behaves, what has already been measured. Do not
   re-derive them.

4. Implement it. Test-first where the ticket has a seam testable outside
   Blender; ~/.agents/skills/tdd/SKILL.md is the reference for what makes a
   test worth keeping.

5. Run these gates and paste the real output into the ticket:

   python3 tests/test_conversation.py

   python3 tools/bounded_run.py 60 -- \\
       $BLENDER --background --factory-startup --python tools/panel_draw_smoke.py

   Gate that last one on the token it prints, never on the exit status: Blender
   exits 0 even when a --python script raises. Grep for SMOKE OK.

   If the ticket touches the network path, additionally:

   set -a; . ./.env; set +a
   python3 tools/bounded_run.py 180 -- \\
       $BLENDER_PY tools/transport_smoke.py

   That makes real billable requests. Never print, echo or log the key, and do
   not do it at all if nothing in the ticket needs it.

6. Tick a checkbox only if a command you ran in THIS session shows it passing.
   Leave it unticked and say what is missing instead. Seven unticked boxes and
   an honest note is a better outcome than eight ticks and a bluff.

7. Append your answer under the ticket's "## Answer" heading, then set Status to
   "resolved". Give: what changed and where, the evidence as command followed by
   observed output, and what you could not verify. If the ticket cannot be
   finished without a human, set Status to "human-required", add a DO NOT CLAIM
   banner above the heading, and say exactly what a human must do.

8. Touch nothing else. No git commands that write. No edits to another ticket -
   you are its only owner. No writes anywhere under
   /Users/user/Projects/blender. You may hand an independent probe to a
   subagent, but you keep the ticket's status line and its answer.

Bound every probe you run. Never run a case whose subject is a hang in the
foreground. Chain checks with && and never with ;, so a failure stops the next
step instead of being scrolled past.

Screens are allowed: run Blender with a window whenever that is the honest way to
check, and take a screenshot when looking at the result answers the question.
Background it, bound it with tools/bounded_run.py, have the script quit Blender
itself, and write results to a file rather than trusting the terminal.

When finished, print a summary of at most 20 lines.
EOF
}

prompt="$(build_prompt)"
mkdir -p "$LOGS"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "would run:"
  echo "  pi -p --name build-$ID \\"
  echo "     --skill $SKILLS/tdd --skill $SKILLS/diagnosing-bugs \\"
  echo "     --skill $SKILLS/prototype"
  echo "     '<$(printf '%s' "$prompt" | wc -l | tr -d ' ')-line prompt>'"
  echo
  echo "--- prompt ---"
  printf '%s\n' "$prompt"
  exit 0
fi

echo ">> logs/build-$ID.log   (one ticket, no parallelism; expect a long wait)"
pi -p \
   --name "build-$ID" \
   --skill "$SKILLS/tdd" \
   --skill "$SKILLS/diagnosing-bugs" \
   --skill "$SKILLS/prototype" \
   "$prompt" > "$LOGS/build-$ID.log" 2>&1 < /dev/null
rc=$?

echo
echo "=== result ==="
newstatus="$(status_of "$ISSUES/$FILE")"
printf '%-4s %-14s %s\n' "$ID" "${newstatus:-<missing>}" \
  "logs/build-$ID.log  (exit $rc, $(wc -l < "$LOGS/build-$ID.log" | tr -d ' ')L)"
if [ "$newstatus" = "claimed" ]; then
  echo "     ^ STALE CLAIM: the executor died mid-ticket. Read the log, reset Status"
  echo "       to 'open', and retry once with the failure quoted in the ticket."
fi
echo
echo "--- what it changed, and whether it is committed ---"
git -C "$REPO" --no-pager diff --stat | tail -5
echo
echo "NOT VERIFIED. You are the check: re-run the ticket's own evidence commands"
echo "and the gates before you commit or trust the status. An executor marking its"
echo "own homework is not a check."
