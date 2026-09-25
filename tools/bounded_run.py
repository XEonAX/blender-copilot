#!/usr/bin/env python3
"""Run a command with a HARD deadline, killing its whole process group on overrun.

    python3 tools/bounded_run.py <seconds> -- <command> [args...]

    python3 tools/bounded_run.py 30 -- \\
        /Applications/Blender.app/Contents/MacOS/Blender --factory-startup -b \\
        -P tools/runaway_probe.py

Why this exists: two pi instances on this effort wedged themselves running a
probe case whose subject *is* an infinite loop. The tool call that launched it
never returned, so the instance could not advance and the runaway had to be
killed by hand from outside. `AGENTS.md` now requires every probe invocation to
be bounded; this is the bounded mechanism, because the obvious idioms are traps:

  * macOS ships no `timeout(1)` (that is GNU coreutils).
  * `perl -e 'alarm 30; exec @ARGV' -- cmd` looks right and is not reliable --
    verified failing here: the alarm did not survive `exec`, the child outlived
    its deadline, and the caller blocked anyway.
  * A shell `cmd & sleep N; kill` needs `sleep`, which is forbidden in this repo
    precisely because it hides the difference between waiting and hanging.

The process *group* is killed rather than the process, so a runaway Blender's
children die with it (`start_new_session=True` makes the child a group leader).

Exit codes: 0 the command finished on its own; 1 it was killed at the deadline;
2 misuse. Either way a single `BOUNDED |` line states what happened, so a caller
never has to infer it from silence.
"""

import os
import signal
import subprocess
import sys
import time


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print(__doc__)
        return 2

    try:
        deadline = float(argv[1])
    except ValueError:
        print(f"not a number of seconds: {argv[1]!r}", file=sys.stderr)
        return 2

    cmd = argv[3:]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, start_new_session=True)

    try:
        rc = proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        elapsed = time.monotonic() - started
        print(
            f"BOUNDED | KILLED at the {deadline:g}s deadline after {elapsed:.1f}s"
            " | the case never returned -- that IS the result for a hang case",
            flush=True,
        )
        return 1

    elapsed = time.monotonic() - started
    print(f"BOUNDED | finished on its own in {elapsed:.1f}s | rc={rc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
