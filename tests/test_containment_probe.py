"""Offline parser checks and a real CPU-only process-group escape demonstration."""
import importlib.util
import os
import array
import select
import socket
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'containment_probe.py'
spec = importlib.util.spec_from_file_location('containment_probe', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ContainmentProbeTests(unittest.TestCase):
    def test_maps_current_membership_to_cgroup2_mount(self):
        mounts = '12 1 0:1 / /sys/fs/cgroup/unified rw - cgroup2 cgroup rw\n'
        self.assertEqual(m.discover_v2('0::/system.slice/test.service\n', mounts), '/sys/fs/cgroup/unified/system.slice/test.service')
        self.assertIsNone(m.discover_v2('0::/../escape\n', mounts))
        self.assertIsNone(m.discover_v2('1:cpu:/x\n', mounts))

    def test_blocks_even_when_capability_hints_are_positive(self):
        with mock.patch.object(m.os, 'access', return_value=True), mock.patch.object(m.shutil, 'which', return_value=None):
            self.assertEqual(m.probe()['verdict'], 'BLOCKED')

    @unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal') and hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'), 'Linux pidfd and waitid(WNOWAIT) required')
    def test_detached_descendant_survives_original_process_group(self):
        # Probe pidfd support on this process only; no foreign numeric PID is used.
        try:
            own_fd = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(own_fd, 0)
            finally:
                os.close(own_fd)
        except OSError as exc:
            self.skipTest('pidfd unavailable: %s' % exc)

        # The leader opens a pidfd for its own unreaped child and passes that
        # exact fd via SCM_RIGHTS. A second socket proves the child can execute
        # after the leader has been killed, not merely that a zombie exists.
        child_code = r"""
import socket, sys
sock = socket.socket(fileno=int(sys.argv[1]))
sock.settimeout(20)
try:
    while True:
        data = sock.recv(1)
        if not data:
            break
        sock.sendall(data)
except (OSError, TimeoutError):
    pass
"""
        leader_code = r"""
import array, os, signal, socket, subprocess, sys
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
control = socket.socket(fileno=int(sys.argv[1]))
life_fd = int(sys.argv[2])
child = None
try:
    child = subprocess.Popen([sys.executable, '-c', sys.argv[3], str(life_fd)],
                             start_new_session=True, pass_fds=(life_fd,))
    os.close(life_fd)
    pidfd = os.pidfd_open(child.pid)
    try:
        fds = array.array('i', [pidfd])
        control.sendmsg([b'R'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
    finally:
        os.close(pidfd)
    control.settimeout(20)
    if control.recv(1) != b'A':
        raise RuntimeError('pidfd handoff was not acknowledged')
    child.wait(timeout=20)
finally:
    if child is not None and child.poll() is None:
        child.kill()
        child.wait(timeout=5)
"""
        parent_control, leader_control = socket.socketpair()
        parent_life, child_life = socket.socketpair()
        parent_control.settimeout(10)
        parent_life.settimeout(5)
        leader = None
        pidfd = None
        try:
            leader = subprocess.Popen(
                [sys.executable, '-c', leader_code, str(leader_control.fileno()),
                 str(child_life.fileno()), child_code],
                start_new_session=True, pass_fds=(leader_control.fileno(), child_life.fileno()))
            leader_control.close()
            child_life.close()
            data, ancillary, flags, _ = parent_control.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
            received = array.array('i')
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    received.frombytes(payload[:len(payload) - len(payload) % received.itemsize])
            if data != b'R' or flags & socket.MSG_CTRUNC or len(received) != 1:
                for unexpected_fd in received:
                    os.close(unexpected_fd)
                self.fail('invalid pidfd transfer')
            pidfd = received[0]
            parent_control.sendall(b'A')
            # WNOWAIT keeps an exited leader unreaped, pinning its process group
            # ID until the signal is sent; no numeric child PID is involved.
            self.assertIsNone(os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(timeout=5)
            parent_life.sendall(b'P')
            self.assertEqual(parent_life.recv(1), b'P', 'detached child did not execute after leader death')
        finally:
            # Closing this peer also lets the child exit if pidfd transfer failed.
            parent_life.close()
            parent_control.close()
            leader_control.close()
            child_life.close()
            child_exited = True
            if pidfd is not None:
                try:
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    poller = select.poll()
                    poller.register(pidfd, select.POLLIN)
                    child_exited = bool(poller.poll(5000))
                finally:
                    os.close(pidfd)
            if leader is not None and leader.returncode is None:
                if pidfd is None:
                    # Closed peers let the leader reap its own child if handoff
                    # failed; only terminate the pinned leader after a timeout.
                    try:
                        leader.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if leader.returncode is None:
                    event = os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if event is None:
                        os.killpg(leader.pid, signal.SIGKILL)
                    leader.wait(timeout=5)
            self.assertTrue(child_exited, 'detached child did not exit after cleanup')
