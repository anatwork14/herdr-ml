import json, os, pathlib, shlex, subprocess, sys
root = pathlib.Path('/mnt/ssd/genos2/workspace/herdr-ml-pueue-test')
env = {'PATH': '/usr/bin:/bin', 'USER': os.environ.get('USER', ''), 'XDG_CONFIG_HOME': str(root/'config'), 'XDG_DATA_HOME': str(root/'data'), 'XDG_RUNTIME_DIR': str(root/'runtime')}
pueue = str(root/'bin/pueue')
events = root/'events.jsonl'
assert not events.exists()
ids = []
for project in ('project-a', 'project-b'):
    command = shlex.join([sys.executable, str(root/'task.py'), str(events), project])
    ids.append(subprocess.check_output([pueue,'add','-g','gpu0','--print-task-id','--',command], env=env, text=True).strip())
subprocess.run([pueue,'wait','--group','gpu0','--quiet'],env=env,check=True,timeout=30)
items = [json.loads(line) for line in events.read_text().splitlines()]
assert [item['event'] for item in items] == ['start','end','start','end'], items
assert set(item['project'] for item in items) == {'project-a','project-b'}
print(json.dumps({'pueue_version':subprocess.check_output([pueue,'--version'],env=env,text=True).strip(),'task_ids':ids,'events':items,'result':'PASS'},indent=2))
