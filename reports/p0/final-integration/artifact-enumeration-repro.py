import importlib.util, pathlib, tempfile
from unittest.mock import patch
path='/mnt/ssd/genos2/workspace/herdr-ml-independent-artifact/scripts/artifact_tree.py'
spec=importlib.util.spec_from_file_location('artifact',path); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as tmp:
 node=pathlib.Path(tmp)
 for depth in range(12):
  for i in range(38): (node/('z%03d'%i)).write_bytes(b'')
  nxt=node/'a';nxt.mkdir();node=nxt
 original=m.os.scandir; count=[0]
 class Counting:
  def __init__(self,fd): self.inner=original(fd)
  def __enter__(self): self.inner.__enter__();return self
  def __exit__(self,*args):return self.inner.__exit__(*args)
  def __iter__(self):
   for entry in self.inner:
    count[0]+=1
    yield entry
 try:
  with patch.object(m.os,'scandir',side_effect=Counting):m.snapshot(tmp,max_entries=50)
 except m.ArtifactError as e: print('result:',str(e),'directory entries enumerated:',count[0], 'limit:',50)
