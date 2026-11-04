"""TEST-ONLY gated launcher. Never import this from scripts/.

A containment test double delegates spawn_gated here. The child is a shim blocked on an
inherited pipe until the boundary releases the gate; if the gate closes unreleased it exits
without running the job. Same pid and start time after exec.
"""
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.append(str(_SCRIPTS))
import containment_backend as cb  # noqa: E402

GATE_SRC = r'''import os, sys
fd = int(sys.argv[1])
go = os.read(fd, 1)
os.close(fd)
if go != b'1':
    os._exit(125)
try:
    os.execvp(sys.argv[2], sys.argv[2:])
except OSError as exc:
    sys.stderr.write(str(exc) + "\n")
    os._exit(127)
'''


def spawn_gated(command, *, cwd, env):
    gate_read, gate_write = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, '-I', '-c', GATE_SRC, str(gate_read)] + list(command),
                                cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True, pass_fds=(gate_read,))
    except BaseException:
        os.close(gate_write)
        raise
    finally:
        os.close(gate_read)
    return cb.GatedChild(proc, gate_write)
