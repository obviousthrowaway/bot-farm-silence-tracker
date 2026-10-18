"""Run a command with a pseudo-terminal as its controlling terminal and type answers.

    python3 run_in_pty.py ANSWER [ANSWER...] -- COMMAND [ARG...]

Each time the command prints a "Choice" prompt, the next ANSWER is typed (followed by
Enter). An ANSWER ending in "^D" is typed without Enter, followed by end-of-file twice
(Ctrl-D), to check that a partly typed answer survives the end of input.
Everything the command prints is copied to stdout; the exit status is the command's.
Stops with status 124 after 60 seconds. If the command asks again after the answers run out,
an empty line is typed, which picks the default.

With PTY_CLEAR_ONLCR=1 in the environment, the terminal's "onlcr" output setting is
turned off when the command starts and again just before each answer is typed, the way
another program sharing the terminal could leave it. Without onlcr, a new line does not
return to the left edge.
"""

import os
import pty
import select
import sys
import termios
import time


def clear_onlcr(fd: int) -> None:
    if os.environ.get("PTY_CLEAR_ONLCR") == "1":
        attrs = termios.tcgetattr(fd)
        attrs[1] &= ~termios.ONLCR
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


def main() -> int:
    split = sys.argv.index("--")
    answers = sys.argv[1:split]
    command = sys.argv[split + 1 :]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)
    clear_onlcr(fd)
    seen = b""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        seen += chunk
        while b"Choice [1]: " in seen:
            seen = seen.split(b"Choice [1]: ", 1)[1]
            reply = answers.pop(0) if answers else ""
            clear_onlcr(fd)
            if reply.endswith("^D"):
                os.write(fd, reply[:-2].encode() + b"\x04\x04")
            else:
                os.write(fd, reply.encode() + b"\n")
    else:
        os.kill(pid, 9)
        return 124
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
