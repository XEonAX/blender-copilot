#!/usr/bin/env bash
#
# Launch one non-interactive `pi` process per wayfinder ticket, in parallel.
#
#   tools/wayfinder-wave.sh --dry-run 04
#   tools/wayfinder-wave.sh 05 06 11 12
#
# Each instance claims its own ticket, resolves it, and logs to logs/<NN>.log.
# Nothing here writes to map.md: the orchestrator sweeps the map in one serial
# pass afterwards, which is what stops concurrent writers clobbering it.
#
# The full working protocol lives in AGENTS.md, which pi loads as a context file
# in every mode - including print mode, and regardless of project trust.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ISSUES="$REPO/.scratch/blender-copilot/issues"
LOGS="$REPO/logs"
SKILLS="${PI_SKILLS_DIR:-$HOME/.agents/skills}"

DRY_RUN=0
TARGETS=""

for arg in "$@"; do
  case "$arg" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "usage: $(basename "$0") [--dry-run] <ticket> [<ticket> ...]"
      echo "  <ticket> is a number (04), a prefix, or a filename"
      exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *)  TARGETS="$TARGETS $arg" ;;
  esac
done

if [ -z "${TARGETS// /}" ]; then
  echo "usage: $(basename "$0") [--dry-run] <ticket> [<ticket> ...]" >&2
  exit 2
fi

command -v pi >/dev/null 2>&1 || { echo "error: pi not on PATH" >&2; exit 1; }
[ -d "$ISSUES" ] || { echo "error: no tickets at $ISSUES" >&2; exit 1; }

mkdir -p "$LOGS"

build_prompt () {
  cat <<EOF
Resolve exactly one wayfinder ticket, following the protocol in AGENTS.md.

Repo root:    $REPO
Map:          .scratch/blender-copilot/map.md
Your ticket:  .scratch/blender-copilot/issues/$1

Do this now, in order:

1. BEFORE any other work, edit your ticket file and change its Status line to
   \`Status: claimed\`. That edit is the claim: other instances are running
   concurrently and will skip a claimed ticket.
2. Read map.md in full, then any research file under
   .scratch/blender-copilot/research/ that your ticket points at.
3. Resolve your ticket: append the answer under its \`## Answer\` heading, then
   set \`Status: resolved\`. Keep the answer decision-dense, not an essay.
4. Touch nothing else: no edits to map.md, no edits to other ticket files, no
   git commands, no writes under /Users/user/Projects/blender.
5. Delegation is allowed (AGENTS.md, 2026-09-26): hand an independent probe to a
   subagent if that helps. You still own this ticket - its status line and its
   answer stay yours.

If your ticket's Type is \`grilling\`: no human is present, so you must still
decide - but record the decision as PROVISIONAL, with the evidence, the
alternatives you rejected and why, and a final line naming exactly what a human
must ratify.

When finished, print a summary of at most 15 lines.
EOF
}

echo "wayfinder wave:$TARGETS"
echo

pids=""; ids=""; launched=0
for target in $TARGETS; do
  file=""
  if [ -f "$ISSUES/$target" ]; then
    file="$target"
  else
    for candidate in "$ISSUES"/"$target"*.md; do
      [ -f "$candidate" ] && file="$(basename "$candidate")" && break
    done
  fi

  if [ -z "$file" ]; then
    echo "!! no ticket matches '$target' - skipping"
    continue
  fi

  id="${file%%-*}"
  prompt="$(build_prompt "$file")"

  echo ">> $file   ->   logs/$id.log"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "   pi -p --name wf-$id \\"
    echo "      --skill $SKILLS/wayfinder --skill $SKILLS/research \\"
    echo "      --skill $SKILLS/prototype --skill $SKILLS/grilling \\"
    echo "      --skill $SKILLS/domain-modeling \\"
    echo "      \"<$(echo "$prompt" | wc -l | tr -d ' ')-line prompt from this script>\""
    continue
  fi

  # stdin is closed deliberately: in print mode pi merges piped stdin into the
  # prompt, so an inherited TTY could make it wait for input that never comes.
  pi -p \
     --name "wf-$id" \
     --skill "$SKILLS/wayfinder" \
     --skill "$SKILLS/research" \
     --skill "$SKILLS/prototype" \
     --skill "$SKILLS/grilling" \
     --skill "$SKILLS/domain-modeling" \
     "$prompt" > "$LOGS/$id.log" 2>&1 < /dev/null &

  pids="$pids $!"
  ids="$ids $id"
  launched=$((launched + 1))
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "dry run - nothing launched"
  exit 0
fi

if [ "$launched" -eq 0 ]; then
  echo "nothing launched"
  exit 1
fi

echo
echo "waiting for $launched instance(s)..."
for pid in $pids; do
  wait "$pid"
  rc=$?
  [ "$rc" -ne 0 ] && echo "!! pid $pid exited $rc"
done

echo
echo "=== result ==="
for id in $ids; do
  file=""
  for candidate in "$ISSUES"/"$id"-*.md; do
    [ -f "$candidate" ] && file="$candidate" && break
  done
  status="missing"
  [ -n "$file" ] && status="$(grep -m1 '^Status:' "$file" | awk '{print $2}')"
  lines="-"
  [ -f "$LOGS/$id.log" ] && lines="$(wc -l < "$LOGS/$id.log" | tr -d ' ')"
  printf '%-6s %-11s %-9s %s\n' "$id" "$status" "${lines}L" "logs/$id.log"
  if [ "$status" = "claimed" ]; then
    echo "       ^ stale claim: the instance died mid-ticket. Reset to 'open', then retry once."
  fi
done
