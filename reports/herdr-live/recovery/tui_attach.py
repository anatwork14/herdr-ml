"""Interactive attach/detach/reattach of a disposable Herdr terminal under a pty.

Usage: python3 tui_attach.py <terminal_id> <out_dir>

Each attach is a separate `herdr terminal attach <id>` process. The detach key is
ctrl+b q, as documented by `herdr terminal --help`. Raw pty output and timing are
written per attach. No production terminal id is accepted by this script's caller.
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


def attach(term_id, out_dir, n, read_seconds):
    record = {"attach": n, "start": now(), "command": ["herdr", "terminal", "attach", term_id]}
    pid, fd = pty.fork()
    if pid == 0:
        import fcntl, struct, termios
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execvp("herdr", ["herdr", "terminal", "attach", term_id])
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
    os.write(fd, b"\x02q")  # ctrl+b, then q
    drain_end = time.time() + 4
    exited = False
    status = None
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
            exited = True
            status = st
            break
    if not exited:
        # Detach did not exit the client; record that and terminate our child only.
        record["detach_timeout"] = True
        os.kill(pid, 15)
        _, status = os.waitpid(pid, 0)
    record["detach_observed_exit"] = exited
    record["exit_status_raw"] = status
    if status is None:
        record["exit_code"] = None
    elif os.WIFEXITED(status):
        record["exit_code"] = os.WEXITSTATUS(status)
    else:
        record["exit_code"] = "signal:%d" % os.WTERMSIG(status)
    record["end"] = now()
    record["bytes_total"] = len(buf)
    with open(os.path.join(out_dir, f"11-tui-attach-{n}.bin"), "wb") as f:
        f.write(bytes(buf))
    with open(os.path.join(out_dir, f"11-tui-attach-{n}.json"), "w") as f:
        json.dump(record, f, indent=2)
    return record


def main():
    term_id, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    gap = 4
    results = []
    results.append(attach(term_id, out_dir, 1, 5))
    time.sleep(gap)
    results.append(attach(term_id, out_dir, 2, 5))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
