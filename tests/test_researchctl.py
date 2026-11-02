"""CPU-only tests: never load models, datasets, agents, queues or GPU."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "researchctl.py"
spec = importlib.util.spec_from_file_location("researchctl", MODULE_PATH)
researchctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(researchctl)


class ResearchCtlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "research"
        self.repo = self.root / "demo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        cfg = Path(self.temp.name) / "projects.json"
        cfg.write_text(json.dumps({"version": 1, "projects": [
            {"id": "demo", "directory": "demo", "label": "Demo", "gpu_group": "gpu0"}
        ]}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"RESEARCH_ROOT": str(self.root),
                                          "HERDR_ML_CONFIG": str(cfg)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, code="print('hello')", mode="diagnostic", **kwargs):
        args = ["init-run", "--project", "demo", "--mode", mode,
                "--dataset-version", "test", "--split-hash", "split-hash",
                "--seed", "42"]
        if kwargs.get("approve"):
            args.append("--approve-official")
        args += ["--", sys.executable, "-c", code]
        return researchctl.main(args)

    def _records(self):
        return list((self.root / ".runtime" / "demo" / "runs").glob("*"))

    def test_diagnostic_success_writes_evidence(self):
        self.assertEqual(self._run(), 2)
        folder, = self._records()
        state = researchctl.load_json(folder / "status.json")
        submission = researchctl.load_json(folder / "submission.json")
        result = researchctl.load_json(folder / "result.json")
        self.assertEqual(state["state"], "UNKNOWN")
        self.assertEqual(result["leader_state"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("stdout_sha256_after_leader_exit", result)
        self.assertIn("stderr_sha256_after_leader_exit", result)
        self.assertNotIn("stdout_sha256", result)
        self.assertNotIn("stderr_sha256", result)
        self.assertEqual(submission["mode"], "diagnostic")
        self.assertIn("hello", (folder / "stdout.log").read_text())
        self.assertEqual(
            researchctl.hashlib.sha256((folder / "submission.json").read_bytes()).hexdigest(),
            (folder / "submission.sha256").read_text().strip())

    def test_failed_run_keeps_logs(self):
        self.assertEqual(self._run("import sys;sys.stderr.write('bad');sys.exit(9)"), 9)
        folder, = self._records()
        self.assertEqual(researchctl.load_json(folder / "status.json")["state"], "UNKNOWN")
        self.assertEqual(researchctl.load_json(folder / "result.json")["leader_state"], "FAILED")
        self.assertIn("bad", (folder / "stderr.log").read_text())

    def test_missing_gate_blocks_official_before_submission(self):
        self.assertEqual(self._run(mode="official", approve=True), 2)
        self.assertEqual(self._records(), [])

    def test_missing_explicit_official_approval_blocks(self):
        self._write_valid_gates()
        self.assertEqual(self._run(mode="official"), 2)

    def _write_valid_gates(self):
        evidence_dir = self.repo / ".research"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "report.md").write_text("# Signed outside runner\n")
        gates = {key: {"status": "PASS", "reviewer": "independent-reviewer",
                       "reviewed_at": "2026-11-02", "evidence": [".research/report.md"]}
                 for key in researchctl.REQUIRED}
        (evidence_dir / "gates.json").write_text(json.dumps({
            "schema_version": 1, "gates": gates}), encoding="utf-8")

    def test_valid_gate_schema_checks_local_evidence(self):
        self._write_valid_gates()
        self.assertTrue(researchctl.gate_report(self.repo)["valid"])

    def test_gate_path_escape_rejected(self):
        self._write_valid_gates()
        path = self.repo / ".research" / "gates.json"
        manifest = json.loads(path.read_text())
        manifest["gates"]["DATA_READY"]["evidence"] = ["../../outside.md"]
        path.write_text(json.dumps(manifest))
        self.assertFalse(researchctl.gate_report(self.repo)["valid"])

    def test_official_requires_pinned_clean_git_even_with_gates(self):
        self._write_valid_gates()
        self.assertEqual(self._run(mode="official", approve=True), 2)
        self.assertEqual(self._records(), [])

    def test_registry_is_authoritative_over_stale_or_missing_json(self):
        self.assertEqual(self._run(), 2)
        folder, = self._records()
        (folder / "status.json").unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(researchctl.main(["status", "--project", "demo"]), 0)
        self.assertIn("UNKNOWN", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(researchctl.main(["reconcile", "--project", "demo", "--run-id", folder.name]), 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "UNKNOWN")
        child = json.loads(output.getvalue())["registry"]["child"]
        self.assertGreater(child["pid"], 0)
        self.assertTrue(child["boot"])
        self.assertTrue(child["start_ticks"])

    def test_worker_cannot_rerun_a_completed_run(self):
        self.assertEqual(self._run(), 2)
        folder, = self._records()
        self.assertEqual(researchctl.main(["_execute", "--project", "demo",
                                           "--run-id", folder.name]), 2)


if __name__ == "__main__":
    unittest.main()
