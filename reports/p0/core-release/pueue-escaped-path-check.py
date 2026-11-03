"""Reproduce a CPU-only isolated harness run from a shell-metacharacter path."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--pueue', required=True)
parser.add_argument('--pueued', required=True)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[3]
with tempfile.TemporaryDirectory(prefix='herdr ; safe ') as temp:
    root = Path(temp)
    (root / 'scripts').mkdir()
    for name in ('pueue_cpu_worker_integration.py', 'run_registry.py'):
        shutil.copy2(repo / 'scripts' / name, root / 'scripts' / name)
    command = [sys.executable, str(root / 'scripts/pueue_cpu_worker_integration.py'),
               '--pueue', args.pueue, '--pueued', args.pueued, '--python', sys.executable]
    result = subprocess.run(command, text=True, capture_output=True, timeout=40)
    print(result.stdout.strip())
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
    raise SystemExit(result.returncode)
