import importlib.util, json, tempfile
from pathlib import Path
from unittest.mock import patch
root=Path('/mnt/ssd/genos2/workspace/herdr-ml-independent-atomic')
s=importlib.util.spec_from_file_location('test_hr',root/'tests/test_herdr_reconcile.py'); t=importlib.util.module_from_spec(s);s.loader.exec_module(t)
m=t.m
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp); checkout=p/'checkout'; (checkout/'.git').mkdir(parents=True); state=p/'state.json'; api=t.FakeHerdr()
 def run():return m.reconcile(api,state,'proj','Project',checkout,['Control'])
 with patch.object(m,'_coordination_lock_path',return_value=p/'locks/global'):
  api.fail_after=2
  try: run()
  except TimeoutError: pass
  api.fail_after=None
  before=json.loads(state.read_text()); assert before['pending']['kind']=='tab'
  def workspaces():
   workspaces.count+=1
   snapshot=[dict(obj) for obj in api.ws]
   if workspaces.count==3: api.ws[0]['label']='different owner with reused id'
   return snapshot
  workspaces.count=0;api.workspaces=workspaces
  try: run()
  except m.ReconcileBlocked as e: print('later blocked:',e)
  after=json.loads(state.read_text())
  print('pending before:',before['pending']);print('pending after:',after['pending']);print('persisted tab:',after['tabs'])
