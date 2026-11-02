import sys,tempfile,threading,pathlib,importlib.util
spec=importlib.util.spec_from_file_location('reviewed','/mnt/ssd/genos2/workspace/herdr-ml-independent-herdr/scripts/herdr_reconcile.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Fake:
 def __init__(self):self.ws=[];self.ts={};self.gate=threading.Barrier(2);self.lock=threading.Lock()
 def identity(self):return 'server'
 def creation_cwd_verified(self):return True
 def workspaces(self):return list(self.ws)
 def tabs(self,w):return list(self.ts[w])
 def create_workspace(self,label,cwd):
  self.gate.wait(timeout=5)
  with self.lock:
   ident='w'+str(len(self.ws)+1);self.ws.append({'id':ident,'label':label,'cwd':cwd});self.ts[ident]=[]
 def create_tab(self,w,label,cwd):
  with self.lock:self.ts[w].append({'id':w+'t'+str(len(self.ts[w])+1),'label':label,'cwd':cwd})
with tempfile.TemporaryDirectory() as t:
 root=pathlib.Path(t);repo=root/'repo';(repo/'.git').mkdir(parents=True); api=Fake();out=[]
 def call(i):
  try:out.append(m.reconcile(api,root/('state%d.json'%i),'demo','Demo',repo,['Control']))
  except Exception as e:out.append(type(e).__name__+': '+str(e))
 threads=[threading.Thread(target=call,args=(i,)) for i in range(2)]
 for x in threads:x.start()
 for x in threads:x.join()
 print('workspace_count=',len(api.ws),'results=',out)
