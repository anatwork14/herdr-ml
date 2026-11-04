import json, os, pathlib, shlex, subprocess
root=pathlib.Path('/mnt/ssd/genos2/workspace/herdr-ml-pueue-test')
python='/mnt/ssd/genos2/.local/share/uv/python/cpython-3.10-linux-x86_64-gnu/bin/python3.10'
probe='/mnt/ssd/genos2/workspace/herdr-ml-p0/scripts/runtime_identity.py'
pueue=str(root/'bin/pueue')
env={'PATH':'/usr/bin:/bin', 'USER':os.environ.get('USER',''), 'XDG_CONFIG_HOME':str(root/'config'), 'XDG_DATA_HOME':str(root/'data'), 'XDG_RUNTIME_DIR':str(root/'runtime')}
expected=subprocess.check_output([python,probe],env=env,text=True)
fingerprint=root/'fingerprint.json'
fingerprint.write_text(expected)
cmd=shlex.join([python,probe,'--expect',str(fingerprint)])
task=subprocess.check_output([pueue,'add','-g','gpu0','--print-task-id','--',cmd],env=env,text=True).strip()
subprocess.run([pueue,'wait','-q',task],env=env,check=True,timeout=30)
status=json.loads(subprocess.check_output([pueue,'status','--json'],env=env,text=True))
result=status['tasks'][task]['status']['Done']['result']
assert result == 'Success',result
print(json.dumps({'task_id':task,'worker_fingerprint_match':True,'queue_result':result,'python_version':json.loads(expected)['python_version']},indent=2))
