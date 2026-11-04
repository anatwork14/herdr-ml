import importlib.util, json, tempfile, contextlib
from pathlib import Path
from unittest.mock import patch
root=Path('/mnt/ssd/genos2/workspace/herdr-ml-independent-atomic')
s=importlib.util.spec_from_file_location('test_hr',root/'tests/test_herdr_reconcile.py');t=importlib.util.module_from_spec(s);s.loader.exec_module(t);m=t.m
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp);checkout=p/'checkout';(checkout/'.git').mkdir(parents=True);state=p/'state.json';api=t.FakeHerdr()
 def run():return m.reconcile(api,state,'proj','Project',checkout,['Control'])
 with patch.object(m,'_coordination_lock_path',return_value=p/'locks/global'):
  api.fail_after=2
  try:run()
  except TimeoutError:pass
  api.fail_after=None;before=json.loads(state.read_text());assert before['pending']['kind']=='tab'
  calls=list(api.calls);api.atomic_parent_guard_verified=lambda:False
  try:run();raise AssertionError('unguarded action was accepted')
  except m.ReconcileBlocked:pass
  assert json.loads(state.read_text())==before and api.calls==calls
  api.atomic_parent_guard_verified=lambda:True
  active=[]; attempts=[];original_workspaces=api.workspaces;original_write=m._write
  @contextlib.contextmanager
  def guard(*args):
   active.append(True)
   try:yield
   finally:active.pop()
  def workspaces():
   workspaces.count+=1;snapshot=[dict(x) for x in original_workspaces()]
   if workspaces.count==3:
    assert active;attempts.append('replacement blocked by guard')
   return snapshot
  workspaces.count=0
  def write(path,data):
   if data.get('pending') is None and data.get('tabs'):assert active
   return original_write(path,data)
  api.parent_guard=guard;api.workspaces=workspaces
  with patch.object(m,'_write',side_effect=write): result=run()
  assert len(attempts)==1 and result['tabs']['Control'] and not active
  print('PASS: no guard -> no mutation; simulated guard blocked replacement; adoption persisted while guarded')
