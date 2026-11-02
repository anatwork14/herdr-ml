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
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = BASE / "config" / "projects.json"
REQUIRED = ("DATA_READY", "BASELINE_READY", "GPU_READY")

# Explicit path enables direct execution and importlib-based test loading.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_registry


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
            registered = run_registry.inspect(folder)
            state = registered if registered is not None else load_json(folder / "status.json")
        except (OSError, json.JSONDecodeError):
            print(f"{folder.name}\tINVALID\tmissing/corrupt record")
            continue
        # Historical terminal JSON/registry records did not verify containment.
        label = state.get("state", "UNKNOWN")
        if label in ("CLAIMED", "DISPATCHING", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"):
            label = "UNKNOWN"
        print(f"{folder.name}\t{label}\t{submission.get('mode')}")
    return 0


def cmd_reconcile(args):
    _, _, runtime = select_project(args.project)
    if not args.run_id or Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        raise ResearchError("Unsafe run ID")
    folder = runtime / "runs" / args.run_id
    if not folder.is_dir():
        raise ResearchError("Unknown run")
    entry = run_registry.inspect(folder)
    if entry is None:
        result = {"state": "UNKNOWN", "reason": "No registry record; legacy or pre-claim run needs investigation"}
    elif entry["state"] in ("CLAIMED", "DISPATCHING", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "UNKNOWN"):
        result = {"state": "UNKNOWN", "reason": "Containment not verified; no automatic recovery", "registry": entry}
    else:
        result = {"state": entry["state"], "registry": entry}
    print(json.dumps(result, indent=2))
    return 2 if result["state"] == "UNKNOWN" else 0


def require_supported_execution(mode, queue, *, submission=False):
    if mode != "diagnostic":
        raise ResearchError("Official or invalid execution BLOCKED: authenticated approval and immutable provenance not implemented")
    if (submission and queue != "local") or (not submission and queue is not None):
        raise ResearchError("Queued execution BLOCKED: process containment and daemon interpreter validation not implemented")


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
    require_supported_execution(args.mode, args.queue)
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
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ResearchError("SIGCHLD disposition is not default; cannot safely pin and reap child identity")
    _, checkout, runtime = select_project(project_id)
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ResearchError("Unsafe run ID")
    folder = runtime / "runs" / run_id
    submission = load_json(folder / "submission.json")
    expected = (folder / "submission.sha256").read_text(encoding="utf-8").strip()
    actual = sha_file(folder / "submission.json")
    require_supported_execution(submission.get("mode"), submission.get("queue"), submission=True)
    if expected != actual or submission.get("project") != project_id:
        raise ResearchError("Submission manifest integrity mismatch")
    status_file = folder / "status.json"
    state = load_json(status_file).get("state")
    # Queued records, including legacy records called "local", remain held.
    if state != "SUBMITTED":
        raise ResearchError(f"Refusing execution: queued or repeated state={state}")
    try:
        token = run_registry.claim(folder, run_id, project_id, actual, state)
    except run_registry.RegistryError as exc:
        raise ResearchError(str(exc)) from exc
    if submission.get("mode") == "official":
        gate = gate_report(checkout, submission.get("gate_manifest"))
        current_git = git_metadata(checkout)
        if not gate["valid"] or current_git != submission.get("git") or current_git.get("dirty") is not False:
            atomic_json(status_file, {"state": "BLOCKED", "updated_at": now(),
                                      "error": "Gate invalid or source checkout changed since submission"})
            raise ResearchError("Official job BLOCKED: gates or pinned Git source changed")
    run_registry.advance(folder, token, "CLAIMED", "DISPATCHING")
    atomic_json(status_file, {"state": "DISPATCHING", "updated_at": now()})
    child = None
    interrupted = False
    interruption_signal = None
    group_owned = False

    def signal_group(sig):
        # The unreaped leader pins its PID/PGID. Never signal after reaping.
        if group_owned:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(signum, _frame):
        nonlocal interrupted, interruption_signal
        interrupted = True
        interruption_signal = interruption_signal or signum
        signal_group(signal.SIGTERM)

    def stop_and_reap():
        nonlocal group_owned
        if group_owned:
            signal_group(signal.SIGKILL)
            # No process-group signal is sent after wait() reaps the leader.
            group_owned = False
            return child.wait()
        return None

    old_handlers = {sig: signal.signal(sig, terminate)
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)}
    try:
        with (folder / "stdout.log").open("wb") as stdout, (folder / "stderr.log").open("wb") as stderr:
            try:
                if interrupted:
                    code = 128 + interruption_signal
                else:
                    child = subprocess.Popen(submission["argv"], cwd=checkout,
                                             stdin=subprocess.DEVNULL, stdout=stdout,
                                             stderr=stderr, shell=False, start_new_session=True)
                    group_owned = True
                    run_registry.record_child(folder, token, child.pid)
                    run_registry.advance(folder, token, "DISPATCHING", "RUNNING")
                    atomic_json(status_file, {"state": "RUNNING", "started_at": now(), "updated_at": now()})
                    if interrupted:
                        signal_group(signal.SIGTERM)
                    deadline = None
                    while True:
                        # WNOWAIT keeps the exited leader as a zombie, preventing
                        # its PID and process group ID from being reused.
                        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        if result is not None:
                            break
                        if interrupted and deadline is None:
                            deadline = time.monotonic() + 3
                        if deadline is not None and time.monotonic() >= deadline:
                            signal_group(signal.SIGKILL)
                        time.sleep(0.05)
                    code = stop_and_reap()
            except OSError as exc:
                if child is not None:
                    stop_and_reap()
                    raise
                stderr.write((str(exc) + "\n").encode("utf-8", errors="replace"))
                code = 127
            except BaseException:
                stop_and_reap()
                raise
        # Process groups cannot establish whether detached descendants survive.
        # Record the leader outcome without asserting whole-workload completion.
        leader_state = "INTERRUPTED" if interrupted else ("COMPLETED" if code == 0 else "FAILED")
        end_state = "UNKNOWN"
        result = {"state": end_state, "leader_state": leader_state,
                  "containment": "UNKNOWN", "exit_code": code, "leader_completed_at": now(),
                  "stdout_sha256": sha_file(folder / "stdout.log"),
                  "stderr_sha256": sha_file(folder / "stderr.log")}
        run_registry.finish(folder, token, result)
        atomic_json(folder / "result.json", result)
        atomic_json(status_file, {"state": end_state, "updated_at": now(), "exit_code": code})
        print(f"{end_state} run={run_id} exit={code} logs={folder}", flush=True)
        return 128 + interruption_signal if interrupted else (2 if code == 0 else (code if code >= 0 else 128 + abs(code)))
    finally:
        stop_and_reap()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    status = sub.add_parser("status")
    status.add_argument("--project", required=True)
    reconcile = sub.add_parser("reconcile", help="Inspect a run without reclaiming ownership")
    reconcile.add_argument("--project", required=True)
    reconcile.add_argument("--run-id", required=True)
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
        if args.action == "reconcile":
            return cmd_reconcile(args)
        if args.action == "gate-status":
            return cmd_gate_status(args)
        if args.action == "init-run":
            return cmd_init_run(args)
        return execute(args.project, args.run_id)
    except (ResearchError, run_registry.RegistryError, sqlite3.Error, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 or later is required", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
