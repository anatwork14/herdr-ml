import sys,tempfile,pathlib,importlib.util,time,multiprocessing,json
spec=importlib.util.spec_from_file_location('reviewed','/mnt/ssd/genos2/workspace/herdr-ml-independent-herdr-recheck/scripts/herdr_reconcile.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Fake:
 def __init__(self,shared): self.s=shared
 def identity(self): return 'server'
 def coordination_verified(self): return True
 def creation_cwd_verified(self): return True
 def workspaces(self): return json.loads(self.s['ws'])
 def tabs(self,w): return json.loads(self.s['ts'])[w]
 def create_workspace(self,label,cwd):
  time.sleep(.3)
  ws=self.workspaces(); wid='w'+str(len(ws)+1);ws.append({'id':wid,'label':label,'cwd':cwd});self.s['ws']=json.dumps(ws);ts=json.loads(self.s['ts']);ts[wid]=[];self.s['ts']=json.dumps(ts)
 def create_tab(self,w,label,cwd):
  ts=json.loads(self.s['ts']);ts[w].append({'id':w+'t1','label':label,'cwd':cwd});self.s['ts']=json.dumps(ts)
def worker(root,shared,start,results,i):
 m._coordination_lock_path=lambda: pathlib.Path(root)/'locks'/'global'
 start.wait(5)
 try:results.put(('ok',m.reconcile(Fake(shared),pathlib.Path(root)/('state%d.json'%i),'project%d'%i,'Demo',pathlib.Path(root)/'repo',['Control'])))
 except Exception as e:results.put((type(e).__name__,str(e)))
if __name__=='__main__':
 ctx=multiprocessing.get_context('fork')
 with tempfile.TemporaryDirectory() as t,ctx.Manager() as manager:
  (pathlib.Path(t)/'repo'/'.git').mkdir(parents=True);shared=manager.dict(ws='[]',ts='{}'); start=ctx.Event();results=ctx.Queue();jobs=[ctx.Process(target=worker,args=(t,shared,start,results,i)) for i in range(2)]
  for p in jobs:p.start()
  start.set()
  for p in jobs:p.join(10)
  out=[results.get(timeout=2) for _ in jobs]; ws=json.loads(shared['ws']);print('workspace_count=',len(ws),'results=',out,'exitcodes=',[p.exitcode for p in jobs]);assert len(ws)==1;assert sum(r[0]=='ok' for r in out)==1;assert sum(r[0]=='ReconcileBlocked' for r in out)==1
