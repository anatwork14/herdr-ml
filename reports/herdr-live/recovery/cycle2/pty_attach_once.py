"""One interactive attach to a terminal in a named Herdr session, then the documented detach.

Usage: python3 pty_attach_once.py <session> <terminal_id> <out_prefix> <read_seconds>

Runs `herdr --session <session> terminal attach <terminal_id>` under a pseudo-terminal
with a 120x40 window, reads output for <read_seconds>, sends ctrl+b then q (the detach
key from `herdr terminal --help`), waits up to 4 s for the client to exit, and writes:
  <out_prefix>.bin   raw bytes received from the attach client
  <out_prefix>.json  command, timestamps, exit code and byte counts
"""
import json
import os
import pty
import select
import sys
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main():
    session, term_id, out_prefix, read_seconds = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    argv = ["herdr", "--session", session, "terminal", "attach", term_id]
    record = {"session": session, "terminal_id": term_id, "command": argv, "start": now()}
    pid, fd = pty.fork()
    if pid == 0:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execvp("herdr", argv)
    buf = bytearray()
    end = time.time() + read_seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    record["bytes_before_detach"] = len(buf)
    record["detach_sent"] = now()
    os.write(fd, b"\x02q")
    drain_end = time.time() + 4
    exited, status = False, None
    while time.time() < drain_end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
                if chunk:
                    buf += chunk
            except OSError:
                pass
        done, st = os.waitpid(pid, os.WNOHANG)
        if done:
            exited, status = True, st
            break
    if not exited:
        record["detach_timeout"] = True
        os.kill(pid, 15)
        _, status = os.waitpid(pid, 0)
    record["detach_observed_exit"] = exited
    if os.WIFEXITED(status):
        record["exit_code"] = os.WEXITSTATUS(status)
    else:
        record["exit_code"] = "signal:%d" % os.WTERMSIG(status)
    record["end"] = now()
    record["bytes_total"] = len(buf)
    with open(out_prefix + ".bin", "wb") as f:
        f.write(bytes(buf))
    with open(out_prefix + ".json", "w") as f:
        json.dump(record, f, indent=2)
    print(json.dumps(record))


if __name__ == "__main__":
    main()
