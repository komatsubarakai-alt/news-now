CREATE TABLE IF NOT EXISTS pub_approvals(approval_id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events,kind TEXT NOT NULL,target_key TEXT NOT NULL,content_hash TEXT NOT NULL,content TEXT NOT NULL,actor TEXT NOT NULL,approved_at TEXT NOT NULL,basis TEXT NOT NULL,proof TEXT NOT NULL,scope TEXT NOT NULL CHECK(scope='isolated_simulation'));
CREATE TABLE IF NOT EXISTS pub_state(approval_id TEXT PRIMARY KEY REFERENCES pub_approvals,status TEXT NOT NULL CHECK(status IN ('approved','needs_recheck','withdrawn','superseded')));
CREATE TABLE IF NOT EXISTS pub_heads(event_id TEXT REFERENCES events,kind TEXT,target_key TEXT,approval_id TEXT REFERENCES pub_approvals,PRIMARY KEY(event_id,kind,target_key));
CREATE TABLE IF NOT EXISTS pub_dependencies(approval_id TEXT REFERENCES pub_approvals,required_id TEXT REFERENCES pub_approvals,PRIMARY KEY(approval_id,required_id));
CREATE TABLE IF NOT EXISTS pub_history(history_id TEXT PRIMARY KEY,approval_id TEXT REFERENCES pub_approvals,operation_id TEXT NOT NULL,old_status TEXT,new_status TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pub_operations(operation_id TEXT PRIMARY KEY,command_hash TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pub_generated(approval_id TEXT PRIMARY KEY REFERENCES pub_approvals,generation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pub_notices(notice_id TEXT PRIMARY KEY,event_id TEXT REFERENCES events,approval_id TEXT REFERENCES pub_approvals,status TEXT NOT NULL,at TEXT NOT NULL,message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pub_evidence_invalidations(invalidation_id TEXT PRIMARY KEY,evidence_id TEXT NOT NULL REFERENCES sync_evidence,actor TEXT NOT NULL,at TEXT NOT NULL,reason TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE);
