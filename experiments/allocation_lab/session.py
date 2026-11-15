"""Durable simulation checkpoint, never an exactly-once real API guarantee."""
import hashlib,json,sqlite3
from .allocation import simulate
from .priority import work_key


def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()

class Session:
    """Commit mock budget, manifest, cursor and result in one transaction."""
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS catalogue(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS checkpoint(singleton INTEGER PRIMARY KEY CHECK(singleton=1),state TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS work_history(key TEXT PRIMARY KEY,job_id TEXT NOT NULL,stage TEXT NOT NULL,payload TEXT NOT NULL);
        ''');self.db.commit()
    def close(self):self.db.close()
    def apply(self,operation_id,plan,jobs,seed,windows,profiles,*,catalogue=(),fail_before_commit=False,**options):
        if not operation_id:raise ValueError('operation ID required')
        for stage in jobs.values():
            for j in stage:
                if not j.get('context_signature'):raise ValueError('article/request context proof required for durable resume')
        request=digest({'plan':plan,'jobs':jobs,'seed':seed,'windows':[w.isoformat() for w in windows],'profiles':profiles,'catalogue':catalogue,'options':options})
        self.db.execute('BEGIN IMMEDIATE')
        try:
            previous=self.db.execute('SELECT input_hash,result FROM operations WHERE id=?',(operation_id,)).fetchone()
            if previous:
                if previous[0]!=request:raise ValueError('operation ID conflict; no overwrite')
                self.db.rollback();return json.loads(previous[1])
            prior=self.db.execute('SELECT state FROM checkpoint WHERE singleton=1').fetchone()
            result=simulate(plan,jobs,seed,windows,profiles,resume=json.loads(prior[0]) if prior else None,**options)
            for items in jobs.values():
                for job in items:
                    key=work_key(job)
                    if self.db.execute('SELECT 1 FROM work_history WHERE key=?',(key,)).fetchone() is None:
                        self.db.execute('INSERT INTO work_history VALUES(?,?,?,?)',(key,job['id'],job['stage'],encode(job)))
            for item in catalogue:
                self.db.execute('INSERT OR IGNORE INTO catalogue VALUES(?,?)',(digest(item),encode(item)))
            self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES(1,?)',(encode(result['checkpoint']),))
            self.db.execute('INSERT INTO operations VALUES(?,?,?)',(operation_id,request,encode(result)))
            if fail_before_commit:raise OSError('simulated checkpoint write failure')
            self.db.commit();return result
        except BaseException:
            self.db.rollback();raise
    def records(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM work_history ORDER BY rowid')]
