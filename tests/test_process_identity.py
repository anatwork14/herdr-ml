"""Read-only process identity is evidence, never permission to reclaim."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts' / 'process_identity.py'
spec = importlib.util.spec_from_file_location('process_identity', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'sys/kernel/random').mkdir(parents=True)
        (self.root / 'sys/kernel/random/boot_id').write_text('boot-a\n')
        (self.root / '42').mkdir()
        self.write_stat('S', '123')

    def write_stat(self, state, start):
        # Linux stat after comm: state is field 3; start time is field 22.
        fields = [state] + ['0'] * 18 + [start] + ['0'] * 5
        (self.root / '42/stat').write_text('42 (name with ) spaces) ' + ' '.join(fields))

    def observe(self, **kwargs):
        identity = {'pid': 42, 'boot': 'boot-a', 'start_ticks': '123'}
        identity.update(kwargs)
        return m.observe_process(identity, 'host-a', proc_root=self.root, current_host='host-a')

    def test_matching_identity_and_zombie_are_distinct(self):
        self.assertEqual(self.observe()['observation'], 'MATCH')
        self.write_stat('Z', '123')
        self.assertEqual(self.observe()['observation'], 'MATCH_ZOMBIE')

    def test_pid_reuse_and_boot_change_are_never_a_match(self):
        self.write_stat('S', '124')
        self.assertEqual(self.observe()['observation'], 'IDENTITY_MISMATCH')
        (self.root / 'sys/kernel/random/boot_id').write_text('boot-b')
        self.assertEqual(self.observe()['observation'], 'IDENTITY_MISMATCH')

    def test_absent_pid_is_only_not_visible(self):
        (self.root / '42/stat').unlink()
        self.assertEqual(self.observe()['observation'], 'NOT_VISIBLE')

    def test_missing_or_untrusted_metadata_is_unknown(self):
        self.assertEqual(self.observe(start_ticks=None)['observation'], 'UNKNOWN')
        self.assertEqual(self.observe(pid=True)['observation'], 'UNKNOWN')
        self.assertEqual(m.observe_process({'pid': 42, 'boot': 'boot-a', 'start_ticks': '123'},
            'host-b', proc_root=self.root, current_host='host-a')['observation'], 'UNKNOWN')
        (self.root / '42/stat').write_text('malformed')
        self.assertEqual(self.observe()['observation'], 'UNKNOWN')

    def test_live_current_process_matches(self):
        import os, socket
        root = Path('/proc')
        if not (root / 'self/stat').is_file():
            self.skipTest('Linux procfs unavailable')
        text = (root / 'self/stat').read_text()
        start = text[text.rfind(')') + 2:].split()[19]
        boot = (root / 'sys/kernel/random/boot_id').read_text().strip()
        observed = m.observe_process({'pid': os.getpid(), 'boot': boot, 'start_ticks': start}, socket.gethostname())
        self.assertEqual(observed['observation'], 'MATCH')
