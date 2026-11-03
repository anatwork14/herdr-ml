"""Reproduce one isolated local CPU diagnostic; expected overall state UNKNOWN."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parents[3]
cli = repo / 'scripts' / 'researchctl.py'
with tempfile.TemporaryDirectory(prefix='herdr-ml-core-cpu-') as temp:
    root = Path(temp)
    checkout = root / 'demo'
    checkout.mkdir()
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    cfg = root / 'projects.json'
    cfg.write_text(json.dumps({'version': 1, 'projects': [{'id': 'demo', 'directory': 'demo', 'gpu_group': 'gpu0'}]}))
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'RESEARCH_ROOT': str(root), 'HERDR_ML_CONFIG': str(cfg)}
    command = [sys.executable, str(cli), 'init-run', '--project', 'demo', '--mode', 'diagnostic',
               '--dataset-version', 'synthetic', '--split-hash', 'diagnostic-only', '--seed', '7', '--',
               sys.executable, '-c', 'print("cpu diagnostic")']
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    runs = list((root / '.runtime/demo/runs').iterdir())
    assert len(runs) == 1
    submission = json.loads((runs[0] / 'submission.json').read_text())
    state = json.loads((runs[0] / 'status.json').read_text())
    output = (runs[0] / 'stdout.log').read_text()
    assert result.returncode == 2 and state['state'] == 'UNKNOWN' and output.strip() == 'cpu diagnostic'
    print(json.dumps({'exit_code': result.returncode, 'state': state['state'],
                      'mode': submission['mode'], 'output': output.strip()}, indent=2))
