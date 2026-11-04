"""Adversarial in-process tests: identity, fencing tokens, queue binding and legacy records.

CPU-only. Every file lives in a tempfile directory; no real queue, home directory,
containment layer or workload is used. Proof objects are synthetic and only
shape-checked; no production issuer exists.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry = _load('run_registry_ownership', 'run_registry.py')
ownership = registry.run_ownership
process_identity = _load('process_identity_ownership', 'process_identity.py')

DIGEST = 'a' * 64
OTHER_DIGEST = 'b' * 64
QUEUE = 'd' * 64
OTHER_QUEUE = 'e' * 64
SHA = '1' * 64
NOW = '2026-11-04T00:00:00Z'
ATTEMPT = 'f' * 32


class SimulatedCrash(BaseException):
    """In-process stand-in for a crash at a named boundary: unwinds, no cleanup code runs."""


class OwnershipBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)

    def run_folder(self, project='project-a', run_id='run-1'):
        folder = self.root / '.runtime' / project / 'runs' / run_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def proof(self, run_id, attempt_id, fence_value, **overrides):
        evidence = {'kind': ownership.WHOLE_WORKLOAD_TERMINATED, 'run_id': run_id,
                    'attempt_id': attempt_id, 'fence': fence_value, 'verifier': 'synthetic-test-shape',
                    'observed_at': NOW, 'evidence_sha256': SHA}
        evidence.update(overrides)
        return evidence

    def bound_queue(self, folder, run_id='run-1', task='task-1', digest=DIGEST):
        token = registry.queue_intent(folder, run_id, digest, QUEUE, _store_home=self.home)
        registry.queue_bind_task(folder, token, task, _store_home=self.home)
        return token

    def queued_claim(self, folder, fence, *, run_id='run-1', task='task-1', worker='worker-A',
                     digest=DIGEST, attempt=None, fault=None):
        return registry.claim(folder, run_id, 'project-a', digest, 'QUEUED',
                              queue_instance=QUEUE, queue_task_id=task, queue_worker_id=worker,
                              queue_fence=fence, queue_attempt_id=attempt,
                              queue_store_home=self.home, _fault=fault)


class IdentityTests(unittest.TestCase):
    def identity(self, **changes):
        fields = dict(run_id='run', project='project', attempt_id=ATTEMPT, manifest_sha256=DIGEST,
                      worker_id='worker-1', fence=1, task_id='task-1')
        fields.update(changes)
        return ownership.ExecutionIdentity(**fields).validate()

    def test_canonical_identity_binds_every_field(self):
        base = self.identity().sha256()
        self.assertEqual(base, self.identity().sha256())
        changes = [dict(run_id='other'), dict(project='other'), dict(attempt_id='0' * 32),
                   dict(manifest_sha256=OTHER_DIGEST), dict(worker_id='worker-2'), dict(fence=2),
                   dict(task_id='task-2'), dict(task_id=None)]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(self.identity(**change).sha256(), base)

    def test_identity_rejects_placeholder_and_malformed_fields(self):
        bad = [dict(manifest_sha256='PENDING_W4'), dict(manifest_sha256='A' * 64),
               dict(fence=0), dict(fence=True), dict(fence='1'), dict(attempt_id='xyz'),
               dict(run_id='run/../escape'), dict(run_id=''), dict(worker_id='bad worker'),
               dict(task_id='bad\x00task'), dict(task_id='')]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ownership.RegistryError):
                self.identity(**change)

    def test_token_round_trip_and_malformed_tokens_rejected(self):
        token = ownership.encode_token(ATTEMPT, 7)
        self.assertEqual(ownership.decode_token(token), (ATTEMPT, 7))
        for bad in ('incorrect', None, 'v1.' + ATTEMPT + '.1', 'v2.' + ATTEMPT + '.0',
                    'v2.' + ATTEMPT.upper() + '.1', 'v2.' + ATTEMPT + '.1 ', 42):
            with self.subTest(token=bad), self.assertRaisesRegex(ownership.RegistryError, 'Stale owner'):
                ownership.decode_token(bad)


class LocalOwnershipTests(OwnershipBase):
    def claim(self, folder=None, **kwargs):
        folder = folder or self.run_folder()
        return registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED', **kwargs)

    def test_claim_records_attempt_fence_and_current_identity(self):
        folder = self.run_folder()
        token = self.claim(folder)
        attempt, fence = ownership.decode_token(token)
        self.assertEqual(fence, 1)
        row = registry.inspect(folder)
        self.assertEqual(row['identity'], 'CURRENT')
        self.assertEqual((row['attempt_id'], row['fence'], row['state']), (attempt, 1, 'CLAIMED'))
        self.assertEqual(row['identity_version'], ownership.IDENTITY_VERSION)
        recomputed = ownership.ExecutionIdentity('run-1', 'project-a', attempt, DIGEST,
                                                 row['worker_id'], 1, None).sha256()
        self.assertEqual(row['identity_sha256'], recomputed)

    def test_second_claim_is_refused_and_state_is_unchanged(self):
        folder = self.run_folder()
        self.claim(folder)
        for _ in range(2):
            with self.assertRaises(registry.RegistryError):
                self.claim(folder)
        self.assertEqual(registry.inspect(folder)['state'], 'CLAIMED')

    def test_forged_foreign_and_stale_tokens_fail_closed_on_every_mutating_call(self):
        folder = self.run_folder()
        token = self.claim(folder)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        attempt, _ = ownership.decode_token(token)
        forged = [ownership.encode_token(attempt, 2), ownership.encode_token('0' * 32, 1), 'incorrect']
        calls = [
            lambda t: registry.advance(folder, t, 'DISPATCHING', 'RUNNING'),
            lambda t: registry.record_child(folder, t, os.getpid()),
            lambda t: registry.finish(folder, t, {'state': 'UNKNOWN', 'exit_code': 0}),
            lambda t: registry.verified_finish(folder, t, self.proof('run-1', attempt, 1),
                                               {'state': 'COMPLETED'}),
            lambda t: registry.supersede(folder, t, self.proof('run-1', attempt, 1)),
        ]
        for bad in forged:
            for call in calls:
                with self.subTest(token=bad), self.assertRaises(registry.RegistryError):
                    call(bad)
        self.assertEqual(registry.inspect(folder)['state'], 'DISPATCHING')
        self.assertIsNone(registry.inspect(folder)['result'])

    def test_verified_completion_refuses_every_absent_or_mismatched_proof(self):
        folder = self.run_folder()
        token = self.claim(folder)
        attempt, fence = ownership.decode_token(token)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
        bad_proofs = [None, 'yes', {},
                      self.proof('other-run', attempt, fence),
                      self.proof('run-1', '0' * 32, fence),
                      self.proof('run-1', attempt, fence + 1),
                      self.proof('run-1', attempt, fence, kind='LEADER_EXITED'),
                      self.proof('run-1', attempt, fence, verifier=''),
                      self.proof('run-1', attempt, fence, evidence_sha256='not-a-digest'),
                      self.proof('run-1', attempt, fence, fence=str(fence))]
        for proof in bad_proofs:
            with self.subTest(proof=proof), self.assertRaises(registry.RegistryError):
                registry.verified_finish(folder, token, proof, {'state': 'COMPLETED', 'exit_code': 0})
        self.assertEqual(registry.inspect(folder)['state'], 'RUNNING')

    def test_leader_exit_is_unknown_and_cannot_later_become_completed_without_proof(self):
        folder = self.run_folder()
        token = self.claim(folder)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
        registry.finish(folder, token, {'state': 'UNKNOWN', 'leader_state': 'COMPLETED', 'exit_code': 0})
        row = registry.inspect(folder)
        self.assertEqual(row['state'], 'UNKNOWN')
        self.assertEqual(row['result']['leader_state'], 'COMPLETED')
        with self.assertRaises(registry.RegistryError):
            registry.verified_finish(folder, token, self.proof('run-1', *ownership.decode_token(token)),
                                     {'state': 'COMPLETED'})
        with self.assertRaises(registry.RegistryError):
            registry.finish(folder, token, {'state': 'COMPLETED'})

    def test_supersede_fences_stale_worker_and_blocks_replay_and_reclaim(self):
        folder = self.run_folder()
        old_token = self.claim(folder)
        attempt, fence = ownership.decode_token(old_token)
        registry.advance(folder, old_token, 'CLAIMED', 'DISPATCHING')
        registry.advance(folder, old_token, 'DISPATCHING', 'RUNNING')
        with self.assertRaises(registry.RegistryError):
            registry.supersede(folder, old_token, self.proof('run-1', attempt, fence + 1))
        self.assertEqual(registry.inspect(folder)['state'], 'RUNNING')
        registry.supersede(folder, old_token, self.proof('run-1', attempt, fence))
        row = registry.inspect(folder)
        self.assertEqual((row['state'], row['fence'], row['identity']), ('FENCED', 2, 'CURRENT'))
        with self.assertRaises(registry.RegistryError):
            registry.finish(folder, old_token, {'state': 'UNKNOWN'})
        with self.assertRaises(registry.RegistryError):
            registry.advance(folder, old_token, 'RUNNING', 'DISPATCHING')
        with self.assertRaises(registry.RegistryError):
            self.claim(folder)
        with self.assertRaises(registry.RegistryError):
            registry.supersede(folder, old_token, self.proof('run-1', attempt, fence))

    def test_supersede_refuses_completed_and_unproven_runs(self):
        folder = self.run_folder()
        token = self.claim(folder)
        attempt, fence = ownership.decode_token(token)
        with self.assertRaises(registry.RegistryError):
            registry.supersede(folder, token, None)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        registry.finish(folder, token, {'state': 'UNKNOWN'})
        registry.supersede(folder, token, self.proof('run-1', attempt, fence))
        self.assertEqual(registry.inspect(folder)['fence'], 2)

    def test_interrupted_commit_leaves_prior_consistent_state(self):
        folder = self.run_folder()
        token = self.claim(folder)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        with self.assertRaises(SimulatedCrash):
            registry.advance(folder, token, 'DISPATCHING', 'RUNNING', _fault=self.crash_at('advance:before_commit'))
        self.assertEqual(registry.inspect(folder)['state'], 'DISPATCHING')
        registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
        self.assertEqual(registry.inspect(folder)['state'], 'RUNNING')

    def test_interrupted_finish_commit_keeps_running_and_writes_no_result(self):
        folder = self.run_folder()
        token = self.claim(folder)
        registry.advance(folder, token, 'CLAIMED', 'DISPATCHING')
        registry.advance(folder, token, 'DISPATCHING', 'RUNNING')
        leader = {'state': 'UNKNOWN', 'leader_state': 'COMPLETED', 'exit_code': 0}
        with self.assertRaises(SimulatedCrash):
            registry.finish(folder, token, leader, _fault=self.crash_at('finish:before_commit'))
        row = registry.inspect(folder)
        self.assertEqual((row['state'], row['result']), ('RUNNING', None))
        registry.finish(folder, token, leader)
        self.assertEqual(registry.inspect(folder)['state'], 'UNKNOWN')

    def test_crash_before_local_claim_commit_leaves_marker_and_no_row(self):
        folder = self.run_folder()
        with self.assertRaises(SimulatedCrash):
            self.claim(folder, _fault=self.crash_at('claim:before_commit'))
        self.assertIsNone(registry.inspect(folder))
        self.assertTrue((folder / 'execution.claim').exists())
        with self.assertRaises(registry.RegistryError):
            self.claim(folder)
        self.assertIsNone(registry.inspect(folder))

    @staticmethod
    def crash_at(name):
        def hook(point):
            if point == name:
                raise SimulatedCrash(point)
        return hook

    def test_corrupt_registry_fails_closed_and_is_not_reset(self):
        folder = self.run_folder()
        garbage = b'this is not a sqlite database' * 64
        (folder / 'registry.sqlite3').write_bytes(garbage)
        with self.assertRaises(registry.RegistryError):
            self.claim(folder)
        self.assertEqual((folder / 'registry.sqlite3').read_bytes(), garbage)
        with self.assertRaises(sqlite3.DatabaseError):
            registry.inspect(folder)
        self.assertEqual((folder / 'registry.sqlite3').read_bytes(), garbage)

    def write_legacy_registry(self, folder, *, tampered=False):
        """Old run table: no attempt, fence, worker or identity columns."""
        db = sqlite3.connect(str(folder / 'registry.sqlite3'))
        try:
            db.execute('''CREATE TABLE run (
                id INTEGER PRIMARY KEY CHECK (id = 1), run_id TEXT NOT NULL,
                project TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, owner TEXT NOT NULL, owner_pid INTEGER NOT NULL,
                owner_host TEXT NOT NULL, owner_boot TEXT, owner_start TEXT,
                queue_task_id TEXT, result_json TEXT)''')
            db.execute("INSERT INTO run VALUES (1,'run-1','project-a',?,'CLAIMED','oldtoken',99,'host',NULL,NULL,NULL,NULL)",
                       (DIGEST,))
            db.commit()
        finally:
            db.close()

    def test_legacy_rows_load_as_fenced_unknown_and_are_never_claimable(self):
        folder = self.run_folder()
        self.write_legacy_registry(folder)
        row = registry.inspect(folder)
        self.assertEqual((row['state'], row['legacy_state'], row['identity']),
                         ('UNKNOWN', 'CLAIMED', 'LEGACY_FENCED'))
        with self.assertRaises(registry.RegistryError):
            self.claim(folder)
        self.assertEqual(registry.inspect(folder)['state'], 'UNKNOWN')

    def test_identity_columns_with_tampered_hash_are_legacy_fenced(self):
        folder = self.run_folder()
        token = self.claim(folder)
        db = registry._connect(folder)
        try:
            db.execute("UPDATE run SET state='COMPLETED', identity_sha256=? WHERE id=1", ('0' * 64,))
        finally:
            db.close()
        row = registry.inspect(folder)
        self.assertEqual((row['identity'], row['state']), ('LEGACY_FENCED', 'UNKNOWN'))
        self.assertEqual(ownership.decode_token(token)[1], 1)

    def test_mixed_legacy_and_new_records_are_each_classified_correctly(self):
        legacy = self.run_folder(run_id='legacy')
        self.write_legacy_registry(legacy)
        fresh = self.run_folder(run_id='fresh')
        token = registry.claim(fresh, 'fresh', 'project-a', DIGEST, 'SUBMITTED')
        self.assertEqual(registry.inspect(legacy)['identity'], 'LEGACY_FENCED')
        self.assertEqual(registry.inspect(fresh)['identity'], 'CURRENT')
        self.assertEqual(registry.inspect(fresh)['state'], 'CLAIMED')
        with self.assertRaises(registry.RegistryError):
            registry.claim(legacy, 'legacy', 'project-a', DIGEST, 'SUBMITTED')
        self.assertTrue(token)

    def test_threads_racing_one_local_claim_have_exactly_one_winner(self):
        folder = self.run_folder()
        results = []
        lock = threading.Lock()
        start = threading.Barrier(10)

        def attempt():
            start.wait()
            try:
                registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
                outcome = 'won'
            except registry.RegistryError:
                outcome = 'lost'
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results.count('won'), 1, results)
        self.assertEqual(registry.inspect(folder)['identity'], 'CURRENT')


class PidReuseTests(OwnershipBase):
    def test_recorded_pid_with_different_start_time_is_a_different_process(self):
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        start = registry._identity()[1]
        base = {'pid': os.getpid(), 'boot': boot, 'start_ticks': start,
                'pid_namespace': os.readlink('/proc/self/ns/pid'),
                'mount_namespace': os.readlink('/proc/self/ns/mnt')}
        self.assertEqual(process_identity.observe_process(base, socket.gethostname())['observation'], 'MATCH')
        reused = dict(base, start_ticks=str(int(start) + 1))
        observed = process_identity.observe_process(reused, socket.gethostname())
        self.assertEqual(observed['observation'], 'IDENTITY_MISMATCH')

    def test_absent_pid_is_not_treated_as_terminated_and_never_reclaims(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=30)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        identity = {'pid': child.pid, 'boot': boot, 'start_ticks': '1',
                    'pid_namespace': os.readlink('/proc/self/ns/pid'),
                    'mount_namespace': os.readlink('/proc/self/ns/mnt')}
        observed = process_identity.observe_process(identity, socket.gethostname())
        self.assertEqual(observed['observation'], 'NOT_VISIBLE')
        self.assertNotIn('TERMINATED', json.dumps(observed).upper())
        folder = self.run_folder()
        registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        with self.assertRaises(registry.RegistryError):
            registry.claim(folder, 'run-1', 'project-a', DIGEST, 'SUBMITTED')
        self.assertEqual(registry.inspect(folder)['state'], 'CLAIMED')


class QueueBindingTests(OwnershipBase):
    def test_same_run_id_cannot_be_bound_by_a_second_project(self):
        folder_a = self.run_folder('project-a', 'run-1')
        folder_b = self.run_folder('project-b', 'run-1')
        registry.queue_intent(folder_a, 'run-1', DIGEST, QUEUE, _store_home=self.home)
        with self.assertRaisesRegex(registry.RegistryError, 'another project'):
            registry.queue_intent(folder_b, 'run-1', DIGEST, QUEUE, _store_home=self.home)
        self.assertIsNone(registry.inspect_queue(folder_b, _store_home=self.home))

    def test_explicit_project_must_match_the_folder_scope(self):
        folder = self.run_folder('project-a', 'run-9')
        with self.assertRaisesRegex(registry.RegistryError, 'different project'):
            registry.queue_intent(folder, 'run-9', DIGEST, QUEUE, project='project-b',
                                  _store_home=self.home)
        self.assertIsNone(registry.inspect_queue(folder, _store_home=self.home))

    def test_wrong_task_to_run_mappings_are_rejected(self):
        folder_one = self.run_folder(run_id='run-1')
        folder_two = self.run_folder(run_id='run-2')
        self.bound_queue(folder_one, 'run-1', task='task-A')
        token_two = registry.queue_intent(folder_two, 'run-2', DIGEST, QUEUE, _store_home=self.home)
        with self.assertRaisesRegex(registry.RegistryError, 'already bound'):
            registry.queue_bind_task(folder_two, token_two, 'task-A', _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(folder_two, 'run-2', DIGEST, QUEUE, 'task-A', 'worker-A',
                                 _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(folder_one, 'run-1', DIGEST, QUEUE, 'task-B', 'worker-A',
                                 _store_home=self.home)

    def test_queued_claim_adopts_queue_attempt_and_rejects_attempt_mismatch(self):
        folder = self.run_folder()
        self.bound_queue(folder)
        fence = registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                     _store_home=self.home)
        queue_attempt = registry.inspect_queue(folder, _store_home=self.home)['attempt_id']
        with self.assertRaisesRegex(registry.RegistryError, 'mismatch'):
            self.queued_claim(folder, fence, attempt='0' * 32)
        self.assertFalse((folder / 'execution.claim').exists())
        token = self.queued_claim(folder, fence)
        self.assertEqual(ownership.decode_token(token), (queue_attempt, fence))
        row = registry.inspect(folder)
        self.assertEqual((row['identity'], row['attempt_id'], row['worker_id'], row['queue_task_id']),
                         ('CURRENT', queue_attempt, 'worker-A', 'task-1'))

    def test_replayed_queue_claim_and_old_fence_are_rejected(self):
        folder = self.run_folder()
        self.bound_queue(folder)
        fence = registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                     _store_home=self.home)
        with self.assertRaisesRegex(registry.RegistryError, 'stale replay'):
            registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                 _store_home=self.home)
        for stale in (fence - 1, fence + 1):
            with self.subTest(fence=stale), self.assertRaises(registry.RegistryError):
                self.queued_claim(folder, stale)
        self.assertIsNone(registry.inspect(folder))
        self.queued_claim(folder, fence)
        with self.assertRaises(registry.RegistryError):
            self.queued_claim(folder, fence)

    def test_claim_from_another_claimant_is_refused_and_leaves_no_marker(self):
        folder = self.run_folder()
        self.bound_queue(folder)
        fence = registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                     _store_home=self.home)
        store = self.home / '.local' / 'state' / 'herdr-ml' / 'queue-registry.sqlite3'
        db = sqlite3.connect(str(store))
        try:
            db.execute("UPDATE queue_binding SET claimant='99999:other-boot:1' WHERE run_id='run-1'")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(registry.RegistryError, 'another claimant'):
            self.queued_claim(folder, fence)
        self.assertFalse((folder / 'execution.claim').exists())
        self.assertIsNone(registry.inspect(folder))

    def test_threads_racing_one_queued_local_claim_have_exactly_one_winner(self):
        folder = self.run_folder()
        self.bound_queue(folder)
        fence = registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                     _store_home=self.home)
        results = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def attempt():
            start.wait()
            try:
                self.queued_claim(folder, fence)
                outcome = 'won'
            except registry.RegistryError:
                outcome = 'lost'
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results.count('won'), 1, results)

    def test_ambiguous_intent_resolves_only_with_matching_recorded_evidence(self):
        folder = self.run_folder()
        token = registry.queue_intent(folder, 'run-1', DIGEST, QUEUE, _store_home=self.home)
        registry.queue_mark_ambiguous(folder, token, 'client timed out', _store_home=self.home)
        no_task = {'kind': ownership.QUEUE_LOOKUP_NO_TASK, 'intent_token': token,
                   'queue_instance': QUEUE, 'lookup_id': 'q-1', 'observed_at': NOW}
        with self.assertRaises(registry.RegistryError):
            registry.queue_resolve_ambiguous(folder, None, _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_resolve_ambiguous(folder, dict(no_task, intent_token='0' * 32),
                                             _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_resolve_ambiguous(folder, dict(no_task, task_id='T-1'), _store_home=self.home)
        self.assertEqual(registry.inspect_queue(folder, _store_home=self.home)['phase'], 'AMBIGUOUS')
        self.assertEqual(registry.queue_resolve_ambiguous(folder, no_task, _store_home=self.home), 'RETRYABLE')
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(folder, token, 'late-task', _store_home=self.home)
        with self.assertRaises(registry.RegistryError):
            registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'late-task', 'worker-A',
                                 _store_home=self.home)

    def test_task_evidence_rebinds_intent_origin_but_never_a_claimed_ambiguity(self):
        folder_two = self.run_folder(run_id='run-2')
        token = registry.queue_intent(folder_two, 'run-2', DIGEST, QUEUE, _store_home=self.home)
        registry.queue_mark_ambiguous(folder_two, token, 'lost response', _store_home=self.home)
        task_evidence = {'kind': ownership.QUEUE_LOOKUP_TASK, 'intent_token': token,
                         'queue_instance': QUEUE, 'lookup_id': 'q-2', 'observed_at': NOW,
                         'task_id': 'T-2'}
        self.assertEqual(registry.queue_resolve_ambiguous(folder_two, task_evidence, _store_home=self.home), 'BOUND')
        self.assertEqual(registry.queue_claim(folder_two, 'run-2', DIGEST, QUEUE, 'T-2', 'worker-A',
                                              _store_home=self.home), 1)

        folder_three = self.run_folder(run_id='run-3')
        self.bound_queue(folder_three, 'run-3', task='T-3')
        registry.queue_claim(folder_three, 'run-3', DIGEST, QUEUE, 'T-3', 'worker-A', _store_home=self.home)
        self.assertEqual(registry.queue_recover(folder_three, _store_home=self.home), 'AMBIGUOUS')
        claimed_evidence = dict(task_evidence, intent_token=registry.inspect_queue(
            folder_three, _store_home=self.home)['intent_token'], task_id='T-3')
        with self.assertRaisesRegex(registry.RegistryError, 'containment-verified'):
            registry.queue_resolve_ambiguous(folder_three, claimed_evidence, _store_home=self.home)

    def test_recovery_fences_unbound_intents_and_unlocal_claims_without_retrying(self):
        folder_intent = self.run_folder(run_id='run-4')
        registry.queue_intent(folder_intent, 'run-4', DIGEST, QUEUE, _store_home=self.home)
        self.assertEqual(registry.queue_recover(folder_intent, _store_home=self.home), 'AMBIGUOUS')
        with self.assertRaises(registry.RegistryError):
            registry.queue_bind_task(folder_intent, registry.inspect_queue(
                folder_intent, _store_home=self.home)['intent_token'], 'retry-task', _store_home=self.home)

        folder_claim = self.run_folder(run_id='run-5')
        self.bound_queue(folder_claim, 'run-5', task='T-5')
        fence = registry.queue_claim(folder_claim, 'run-5', DIGEST, QUEUE, 'T-5', 'worker-A',
                                     _store_home=self.home)
        self.assertEqual(registry.queue_recover(folder_claim, _store_home=self.home), 'AMBIGUOUS')
        with self.assertRaises(registry.RegistryError):
            self.queued_claim(folder_claim, fence, run_id='run-5', task='T-5')
        self.assertIsNone(registry.inspect(folder_claim))

    def test_recovery_keeps_a_claim_with_a_matching_local_record(self):
        folder = self.run_folder()
        self.bound_queue(folder)
        fence = registry.queue_claim(folder, 'run-1', DIGEST, QUEUE, 'task-1', 'worker-A',
                                     _store_home=self.home)
        self.queued_claim(folder, fence)
        self.assertEqual(registry.queue_recover(folder, _store_home=self.home), 'CLAIMED')
        self.assertIsNone(registry.queue_recover(self.run_folder('project-z', 'unknown'), _store_home=self.home))

    def test_legacy_queue_index_schema_fails_closed(self):
        store_dir = self.home / '.local' / 'state' / 'herdr-ml'
        store_dir.mkdir(parents=True, mode=0o700)
        (self.home / '.local' / 'state').chmod(0o700)
        store = store_dir / 'queue-registry.sqlite3'
        db = sqlite3.connect(str(store))
        db.execute('''CREATE TABLE queue_binding (run_id TEXT PRIMARY KEY, queue_instance TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL, intent_token TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL, task_id TEXT, worker_id TEXT, fence TEXT, run_folder TEXT NOT NULL UNIQUE)''')
        db.execute("INSERT INTO queue_binding VALUES ('run-1',?,?,'tok','CLAIMED','task-1','w','1','x')",
                   (QUEUE, DIGEST))
        db.commit()
        db.close()
        store.chmod(0o600)
        folder = self.run_folder()
        with self.assertRaisesRegex(registry.RegistryError, 'Legacy queue index'):
            registry.queue_intent(folder, 'run-1', DIGEST, QUEUE, _store_home=self.home)
        with self.assertRaisesRegex(registry.RegistryError, 'Legacy queue index'):
            registry.queue_recover(folder, _store_home=self.home)

    def test_corrupt_queue_index_fails_closed_without_reset(self):
        store_dir = self.home / '.local' / 'state' / 'herdr-ml'
        store_dir.mkdir(parents=True, mode=0o700)
        (self.home / '.local' / 'state').chmod(0o700)
        store = store_dir / 'queue-registry.sqlite3'
        garbage = b'not a queue database' * 40
        store.write_bytes(garbage)
        store.chmod(0o600)
        with self.assertRaises(registry.RegistryError):
            registry.queue_intent(self.run_folder(), 'run-1', DIGEST, QUEUE, _store_home=self.home)
        self.assertEqual(store.read_bytes(), garbage)


class DocumentationConsistencyTests(unittest.TestCase):
    """The protocol document must name every state, phase and crash boundary the code uses."""

    def test_state_machine_doc_names_every_state_phase_and_crash_point(self):
        doc = (Path(__file__).resolve().parents[1] / 'reports' / 'experiment-infra' / 'RUN_OWNERSHIP.md').read_text()
        names = (set(ownership.LOCAL_STATES) | set(ownership.QUEUE_PHASES) | set(ownership.CRASH_POINTS) |
                 {'CANCEL_REQUESTED', 'RELEASED', 'LEGACY_FENCED', 'RETRYABLE'})
        missing = sorted(name for name in names if name not in doc)
        self.assertEqual(missing, [])
        self.assertIn('OFFLINE_TESTED', doc)
        self.assertIn('BLOCKED', doc)
        self.assertNotIn('OPERATIONALLY_READY: PASS', doc)

    def test_unknown_crash_point_names_are_rejected_by_the_hook(self):
        with self.assertRaises(ownership.RegistryError):
            ownership.fault(lambda point: None, 'claim:before_comit')
        ownership.fault(None, 'claim:before_comit')


if __name__ == '__main__':
    unittest.main()
