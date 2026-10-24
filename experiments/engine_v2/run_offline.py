"""Run current + prototype suites on a disposable copy; block real networking."""
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[2]
def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*.json') if '.git' not in p.parts}

def main():
    baseline=hashes(SOURCE);blocked=[]
    def deny(*a,**kw):
        blocked.append(True);raise AssertionError('real networking forbidden')
    with tempfile.TemporaryDirectory(prefix='news-now-shadow-') as temporary:
        repo=Path(temporary)/'repo'
        shutil.copytree(SOURCE,repo,ignore=shutil.ignore_patterns('.git','__pycache__'))
        original=hashes(repo);os.chdir(repo)
        sys.path[:0]=[str(repo),str(repo/'tests'),str(repo/'experiments/engine_v2')]
        sys.dont_write_bytecode=True
        for name in ('AI_TRACKING_DAILY_RESERVED_USD','AI_TRACKING_DAILY_RESERVED_CALLS','AI_TRACKING_RUN_RESERVED_CALLS'):
            os.environ[name]='0'
        with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
            old=unittest.defaultTestLoader.discover(str(repo/'tests'))
            new=unittest.defaultTestLoader.discover(str(repo/'experiments/engine_v2'),pattern='test_*.py',top_level_dir=str(repo/'experiments/engine_v2'))
            result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([old,new]))
        unchanged=baseline==hashes(SOURCE) and original==hashes(repo)
        print(json.dumps({'tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),
                          'network_attempts':len(blocked),'json_hashes_unchanged':unchanged}))
        if not result.wasSuccessful() or blocked or not unchanged:raise SystemExit(1)

if __name__=='__main__':main()
