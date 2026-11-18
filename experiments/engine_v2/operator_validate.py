"""Copy input, block network, run both suites, verify production JSON hashes."""
import hashlib,json,os,shutil,socket,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
source=Path(__file__).resolve().parents[2]
def hashes(root):
 return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json') if 'experiments' not in p.relative_to(root).parts and '.git' not in p.parts}
original=hashes(source);blocked=[]
def deny(*args,**kwargs):blocked.append('attempt');raise AssertionError('network prohibited')
with tempfile.TemporaryDirectory(prefix='news-operator-tests-') as tmp:
 root=Path(tmp)/'repo';shutil.copytree(source,root,ignore=shutil.ignore_patterns('.git','__pycache__'));before=hashes(root);os.chdir(root)
 sys.path[:0]=[str(root),str(root/'tests'),str(root/'experiments/engine_v2')];sys.dont_write_bytecode=True
 with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
  suite=unittest.TestSuite([unittest.TestLoader().discover(str(root/'tests')),unittest.TestLoader().discover(str(root/'experiments/engine_v2'))])
  result=unittest.TextTestRunner(verbosity=1).run(suite)
 summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),network_attempts=len(blocked),production_json_files=len(original),json_hashes_unchanged=before==hashes(root) and original==hashes(source))
 print(json.dumps(summary))
 if not result.wasSuccessful() or blocked or not summary['json_hashes_unchanged']:sys.exit(1)
