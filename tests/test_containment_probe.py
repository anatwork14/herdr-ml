"""Offline parser checks and a real CPU-only process-group escape demonstration."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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

    @unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Linux pidfd required for safe process identity')
    def test_detached_descendant_survives_original_process_group(self):
        # This deliberately tests only isolated CPU subprocesses. pidfd makes
        # cleanup target the exact process, even if a PID is later reused.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'child.pid'
            grandchild = 'import os,pathlib,time; p=pathlib.Path(%r); t=p.with_suffix(".tmp"); t.write_text(str(os.getpid())); os.replace(t,p); time.sleep(30)' % str(marker)
            leader_code = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",%r],start_new_session=True); time.sleep(30)' % grandchild
            leader = subprocess.Popen([sys.executable, '-c', leader_code], start_new_session=True)
            pidfd = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), 'isolated child did not start')
                pid = int(marker.read_text())
                pidfd = os.pidfd_open(pid)
                os.killpg(leader.pid, signal.SIGKILL)
                leader.wait(timeout=5)
                # A successful signal 0 through the pidfd checks this exact process.
                signal.pidfd_send_signal(pidfd, 0)
            finally:
                if pidfd is not None:
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.close(pidfd)
                if leader.poll() is None:
                    os.killpg(leader.pid, signal.SIGKILL)
                    leader.wait(timeout=5)
