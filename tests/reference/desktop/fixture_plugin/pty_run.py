"""Run a plug-in command on a pseudo-terminal and answer its confirmation prompt with yes.

Usage: python desktop/fixture_plugin/pty_run.py <command> [arguments...]
"""

import os
import pty
import select
import signal
import sys
import time

PROMPT = b"Type yes to confirm this entry: "
TIMEOUT_SECONDS = 120


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    pid, terminal = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    output = b""
    answered = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        ready, _, _ = select.select([terminal], [], [], 1)
        if not ready:
            continue
        try:
            chunk = os.read(terminal, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        sys.stdout.buffer.write(chunk)
        sys.stdout.flush()
        if not answered and PROMPT in output:
            os.write(terminal, b"yes\n")
            answered = True
    else:
        os.kill(pid, signal.SIGKILL)
        print(f"\nERROR: the command did not finish in {TIMEOUT_SECONDS} seconds", file=sys.stderr)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if not answered:
        print("\nERROR: the command did not ask for confirmation", file=sys.stderr)
        return code or 1
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
