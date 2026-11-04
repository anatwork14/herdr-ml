import json, os, pathlib, shlex, signal, subprocess, sys, time
root=pathlib.Path('/mnt/ssd/genos2/workspace/herdr-ml-pueue-test')
case=root/'crash-case'
for name in ('config','data','runtime'): (case/name).mkdir(parents=True,exist_ok=True)
env={'PATH':'/usr/bin:/bin','USER':os.environ.get('USER',''), 'XDG_CONFIG_HOME':str(case/'config'),'XDG_DATA_HOME':str(case/'data'),'XDG_RUNTIME_DIR':str(case/'runtime')}
pueue=str(root/'bin/pueue'); daemon=str(root/'bin/pueued-x86_64-unknown-linux-musl')
marker=case/'child.pid'; child=None; service=None

def call(*args): return subprocess.check_output([pueue,*args],env=env,text=True,stderr=subprocess.PIPE)
def start():
    process=subprocess.Popen([daemon],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        try: call('status','--json'); return process
        except subprocess.CalledProcessError:
            if process.poll() is not None: raise RuntimeError('daemon exited early')
            time.sleep(.1)
    raise RuntimeError('daemon did not start')

try:
    service=start()
    payload='import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)' % str(marker)
    task=call('add','--print-task-id','--',shlex.join([sys.executable,'-c',payload])).strip()
    deadline=time.monotonic()+10
    while not marker.exists() and time.monotonic()<deadline: time.sleep(.05)
    if not marker.exists(): raise RuntimeError('task did not begin')
    child=int(marker.read_text()); os.kill(service.pid,signal.SIGKILL); service.wait(timeout=5); service=None
    service=start()
    state=json.loads(call('status','--json'))['tasks'][task]['status']
    print(json.dumps({'test':'CPU-only isolated daemon SIGKILL and restart','task_id':task,'post_restart_state':state,'interpretation':'Pueue observation only; no researchctl reconciliation or containment proof'},indent=2))
finally:
    if service is not None:
        try: call('shutdown'); service.wait(timeout=5)
        except Exception:
            service.kill(); service.wait()
    if child:
        try:
            command=pathlib.Path('/proc/%d/cmdline'%child).read_bytes()
            if str(marker).encode() in command: os.kill(child,signal.SIGKILL)
        except (FileNotFoundError,ProcessLookupError): pass
