#!/usr/bin/env python3
"""Local research run ledger + optional Pueue queue; Python stdlib only.

Not an agent sandbox, isolation boundary, or independent scientific certifier.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import uuid

BASE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = BASE / "config" / "projects.json"
REQUIRED = ("DATA_READY", "BASELINE_READY", "GPU_READY")


class ResearchError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".pending-", delete=False) as temp:
        json.dump(payload, temp, indent=2, sort_keys=True)
        temp.write("\n")
        staged = Path(temp.name)
    os.replace(staged, path)


def settings():
    cfg = load_json(Path(os.getenv("HERDR_ML_CONFIG", str(DEFAULT_CONFIG))).expanduser())
    if cfg.get("version") != 1 or not isinstance(cfg.get("projects"), list):
        raise ResearchError("Invalid projects manifest")
    return cfg


def select_project(project_id):
    selected = next((p for p in settings()["projects"] if p.get("id") == project_id), None)
    if selected is None:
        raise ResearchError(f"Unknown project: {project_id}")
    directory = selected.get("directory", "")
    if not directory or Path(directory).name != directory or directory in (".", ".."):
        raise ResearchError("Unsafe project directory in config")
    if not project_id.replace("-", "").replace("_", "").isalnum():
        raise ResearchError("Unsafe project ID")
    root = Path(os.getenv("RESEARCH_ROOT", str(Path.home() / "research"))).expanduser().resolve()
    return selected, root / directory, root / ".runtime" / project_id


def git_metadata(checkout):
    if not (checkout / ".git").exists():
        return {"sha": None, "dirty": None}
    def git(*args):
        result = subprocess.run(["git", "-C", str(checkout), *args],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    sha = git("rev-parse", "HEAD")
    changed = git("status", "--porcelain")
    return {"sha": sha, "dirty": None if changed is None else bool(changed)}


def gate_report(checkout, gate_file=None):
    path = Path(gate_file).expanduser() if gate_file else checkout / ".research" / "gates.json"
    if not path.is_file():
        return {"valid": False, "path": str(path), "problems": ["Missing gate file"]}
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "path": str(path), "problems": [f"Unreadable gate JSON: {exc}"]}
    entries = data.get("gates", {})
    errors = []
    if data.get("schema_version") != 1 or not isinstance(entries, dict):
        errors.append("Invalid gate schema")
        entries = {}
    for gate in REQUIRED:
        entry = entries.get(gate, {})
        if not isinstance(entry, dict) or entry.get("status") != "PASS":
            errors.append(f"{gate}: missing PASS verdict")
            continue
        if not entry.get("reviewer") or not entry.get("reviewed_at"):
            errors.append(f"{gate}: missing reviewer/date")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{gate}: missing evidence")
        else:
            for item in evidence:
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"{gate}: invalid evidence item")
                    continue
                source = (checkout / item).resolve()
                if not source.is_relative_to(checkout.resolve()) or not source.is_file():
                    errors.append(f"{gate}: evidence missing/outside checkout: {item}")
    return {"valid": not errors, "path": str(path), "problems": errors}


def cmd_doctor(_args):
    print("Herdr ML diagnostics (no installs or GPU allocation):")
    for binary in ("python3", "git", "jq", "herdr", "pueue", "pueued", "uv",
                   "mlflow", "nvidia-smi", "nvtop", "btop", "docker"):
        print(f"  {binary:12} {shutil.which(binary) or 'not found (optional)'}")
    if shutil.which("herdr"):
        r = subprocess.run(["herdr", "status", "server"], capture_output=True, text=True)
        print(f"  herdr server: {'reachable' if r.returncode == 0 else 'not reachable'}")
    if shutil.which("pueue"):
        r = subprocess.run(["pueue", "status"], capture_output=True, text=True)
        print(f"  pueue daemon: {'reachable' if r.returncode == 0 else 'not reachable'}")
    print(f"  RESEARCH_ROOT: {os.getenv('RESEARCH_ROOT', str(Path.home() / 'research'))}")
    return 0


def cmd_gate_status(args):
    _, checkout, _ = select_project(args.project)
    result = gate_report(checkout, args.gates)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 2


def cmd_status(args):
    _, _, runtime = select_project(args.project)
    runs = runtime / "runs"
    if not runs.is_dir():
        print("No runs found")
        return 0
    for folder in sorted(runs.iterdir()):
        if not folder.is_dir():
            continue
        try:
            submission = load_json(folder / "submission.json")
            state = load_json(folder / "status.json")
        except (OSError, json.JSONDecodeError):
            print(f"{folder.name}\tINVALID\tmissing/corrupt record")
            continue
        print(f"{folder.name}\t{state.get('state', 'UNKNOWN')}\t{submission.get('mode')}")
    return 0


def cmd_init_run(args):
    project, checkout, runtime = select_project(args.project)
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv:
        raise ResearchError("Supply executable and argv after --")
    if not (checkout / ".git").exists():
        raise ResearchError(f"Checkout missing: {checkout}; clone before running")
    if args.mode == "official":
        gate = gate_report(checkout, args.gates)
        if not gate["valid"]:
            raise ResearchError("Official run BLOCKED: " + "; ".join(gate["problems"]))
        if not args.approve_official:
            raise ResearchError("Official run requires --approve-official")
    if args.queue and args.queue != project.get("gpu_group", "gpu0"):
        raise ResearchError("Queue must match project GPU group (shared across all projects)")
    if args.queue and not args.approve_cost:
        raise ResearchError("Queued execution requires --approve-cost")
    git = git_metadata(checkout)
    if args.mode == "official" and (not git["sha"] or git["dirty"] is not False):
        raise ResearchError("Official runs require a clean, pinned Git checkout")

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    folder = runtime / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    submission = {
        "schema_version": 1, "run_id": run_id, "project": args.project,
        "mode": args.mode, "submitted_at": now(), "argv": argv,
        "working_directory": str(checkout), "git": git,
        "dataset_version": args.dataset_version, "split_hash": args.split_hash,
        "seed": args.seed, "queue": args.queue or "local",
        "python": sys.version.split()[0], "platform": platform.platform(),
        "gate_manifest": (str(Path(args.gates).resolve()) if args.gates else
                          str(checkout / ".research" / "gates.json")),
    }
    manifest = folder / "submission.json"
    atomic_json(manifest, submission)
    digest = sha_file(manifest)
    (folder / "submission.sha256").write_text(digest + "\n", encoding="utf-8")
    atomic_json(folder / "status.json", {"state": "QUEUED" if args.queue else "SUBMITTED",
                                         "updated_at": now()})
    cmd = [sys.executable, str(Path(__file__).resolve()), "_execute",
           "--project", args.project, "--run-id", run_id]
    if args.queue:
        if not shutil.which("pueue"):
            atomic_json(folder / "status.json", {"state": "SUBMISSION_FAILED",
                                                 "updated_at": now(), "error": "pueue missing"})
            raise ResearchError(f"Run {run_id}: Pueue not installed")
        # Pueue workers may not inherit the submitter's custom project root/config.
        cmd = ["env", "RESEARCH_ROOT=" + str(runtime.parent.parent),
               "HERDR_ML_CONFIG=" + str(Path(os.getenv("HERDR_ML_CONFIG",
                                                     str(DEFAULT_CONFIG))).expanduser().resolve()), *cmd]
        launch = subprocess.run(["pueue", "add", "-g", args.queue, "--", shlex.join(cmd)],
                                capture_output=True, text=True, check=False)
        if launch.returncode:
            atomic_json(folder / "status.json", {"state": "SUBMISSION_FAILED",
                         "updated_at": now(), "error": launch.stderr.strip()[:2000]})
            raise ResearchError(f"Run {run_id}: queue failed: {launch.stderr.strip()[:300]}")
        print(f"QUEUED run={run_id} folder={folder}")
        print(launch.stdout.strip())
        return 0
    print(f"STARTING run={run_id} folder={folder}", flush=True)
    return execute(args.project, run_id)


def execute(project_id, run_id):
    _, checkout, runtime = select_project(project_id)
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ResearchError("Unsafe run ID")
    folder = runtime / "runs" / run_id
    submission = load_json(folder / "submission.json")
    expected = (folder / "submission.sha256").read_text(encoding="utf-8").strip()
    actual = sha_file(folder / "submission.json")
    if expected != actual or submission.get("project") != project_id:
        raise ResearchError("Submission manifest integrity mismatch")
    status_file = folder / "status.json"
    state = load_json(status_file).get("state")
    if state not in ("QUEUED", "SUBMITTED"):
        raise ResearchError(f"Refusing repeated execution: state={state}")
    # Never remove this claim automatically: a killed worker may still own work.
    # mkdir is an atomic, durable-at-the-filesystem-namespace claim, unlike
    # a read followed by os.replace. Recovery needs explicit reconciliation.
    try:
        (folder / "execution.claim").mkdir(mode=0o700)
    except FileExistsError:
        raise ResearchError("Execution already claimed; reconcile before retry")
    if submission.get("mode") == "official":
        gate = gate_report(checkout, submission.get("gate_manifest"))
        current_git = git_metadata(checkout)
        if not gate["valid"] or current_git != submission.get("git") or current_git.get("dirty") is not False:
            atomic_json(status_file, {"state": "BLOCKED", "updated_at": now(),
                                      "error": "Gate invalid or source checkout changed since submission"})
            raise ResearchError("Official job BLOCKED: gates or pinned Git source changed")
    atomic_json(status_file, {"state": "RUNNING", "started_at": now(), "updated_at": now()})
    child = None
    interrupted = False

    def terminate(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        if child and child.poll() is None:
            child.terminate()

    old_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            try:
                child = subprocess.Popen(submission["argv"], cwd=checkout,
                                         stdin=subprocess.DEVNULL, stdout=stdout,
                                         stderr=stderr, shell=False)
                code = child.wait()
            except OSError as exc:
                stderr.write((str(exc) + "\n").encode("utf-8", errors="replace"))
                code = 127
        end_state = "INTERRUPTED" if interrupted else ("COMPLETED" if code == 0 else "FAILED")
        result = {"state": end_state, "exit_code": code, "completed_at": now(),
                  "stdout_sha256": sha_file(folder / "stdout.log"),
                  "stderr_sha256": sha_file(folder / "stderr.log")}
        atomic_json(folder / "result.json", result)
        atomic_json(status_file, {"state": end_state, "updated_at": now(), "exit_code": code})
        print(f"{end_state} run={run_id} exit={code} logs={folder}", flush=True)
        return code if code >= 0 else 128 + abs(code)
    finally:
        signal.signal(signal.SIGTERM, old_handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    status = sub.add_parser("status")
    status.add_argument("--project", required=True)
    gates = sub.add_parser("gate-status")
    gates.add_argument("--project", required=True)
    gates.add_argument("--gates")
    run = sub.add_parser("init-run")
    run.add_argument("--project", required=True)
    run.add_argument("--mode", choices=("diagnostic", "official"), required=True)
    run.add_argument("--dataset-version", required=True)
    run.add_argument("--split-hash", required=True)
    run.add_argument("--seed", type=int, required=True)
    run.add_argument("--gates")
    run.add_argument("--queue")
    run.add_argument("--approve-cost", action="store_true")
    run.add_argument("--approve-official", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    worker = sub.add_parser("_execute", help=argparse.SUPPRESS)
    worker.add_argument("--project", required=True)
    worker.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            return cmd_doctor(args)
        if args.action == "status":
            return cmd_status(args)
        if args.action == "gate-status":
            return cmd_gate_status(args)
        if args.action == "init-run":
            return cmd_init_run(args)
        return execute(args.project, args.run_id)
    except (ResearchError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
