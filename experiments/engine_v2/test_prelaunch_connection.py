"""Fictional pre-AI → existing guard/sync/publication rehearsal, no adapter enablement.

All original versions go to their own evidence/decision checks. Aliases are never
used as an AI-cache hit or as proof that two events are identical.
"""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import test_engine as fixtures
from engine import events,dumps
from event_tracker import TRACKING_RESPONSE
from test_tracking import NOW,report
from pending_triage import fingerprint,group_key
from operator_review import active_selection,digest as operator_digest
from failed_response import digest
import pre_ai as pre
import revision_sync as sync
import publication as pub
import end_to_end as flow

class Connection(unittest.TestCase):
    write=fixtures.PrototypeTests.write
    tearDown=fixtures.PrototypeTests.tearDown
    def setUp(self):
        fixtures.PrototypeTests.setUp(self);sync.initialize(self.db);pub.initialize(self.db);flow.initialize(self.db)
        self.a=copy.deepcopy(self.g['articles'][0]);self.a['rss_source']={'name':'架空配信元'}
        self.b={**copy.deepcopy(self.a),'url':'https://example.test/z-duplicate'}
        self.ga={**copy.deepcopy(self.g),'articles':[self.a]};self.gb={**copy.deepcopy(self.g),'articles':[self.b]}
        self.groups=[self.ga,self.gb];self.projection=pre.plan(self.groups);self.jobs={}
        self.assertEqual(len([c for c in self.projection['cohorts'] if c['active']]),1)
        sync.seed(self.db,self.e['articles'][0])
        for a in [self.a,self.b]:sync.seed(self.db,a,body_text='SYNTHETIC '+a['title'])
    def proof(self,a,event_id='e1'):return sync.add_evidence(self.db,event_id,a['url'],'title',a['title'])
    def adopt(self,a,op,evidence=None):
        g={**self.ga,'articles':[a]};jid=sync.make_job(self.db,'e1',g,evidence or [self.proof(a)],NOW)
        value=copy.deepcopy(TRACKING_RESPONSE);value.update(report(a))
        sync.call_mock(self.db,jid,lambda prompt:value,NOW)
        self.jobs[op]=jid
        return sync.adopt(self.db,op,jid,NOW)
    def test_projecting_does_not_modify_events_or_groups(self):
        before=events(self.db);original=copy.deepcopy(self.groups);pre.plan(self.groups)
        self.assertEqual(events(self.db),before);self.assertEqual(self.groups,original)
    def test_explicit_distinct_constraint_blocks_before_tracking(self):
        r=pre.plan(self.groups,separate_pairs=[[pre.version_id(self.a),pre.version_id(self.b)]])
        self.assertFalse(r['cohorts'][0]['active']);self.assertEqual(len(r['work_groups']),2)
    def test_existing_event_bindings_block_conflicting_representative(self):
        r=pre.plan(self.groups,event_bindings={pre.version_id(self.a):['e1'],pre.version_id(self.b):['e2']})
        self.assertFalse(r['cohorts'][0]['active'])
    def test_operator_selection_matches_only_original_group_and_version(self):
        selection={'action':'same_event','event_id':'e1','source_fingerprint':fingerprint(self.ga),'group_hash':operator_digest(self.ga),'decision_id':'fictional-operator'}
        triage={group_key(self.ga):{'operator_review':selection}}
        self.assertEqual(active_selection(self.ga,triage),selection)
        self.assertIsNone(active_selection(self.gb,triage))
        r=pre.plan(self.groups,event_bindings={pre.version_id(self.a):['e1'],pre.version_id(self.b):['e2']})
        self.assertFalse(r['cohorts'][0]['active']);self.assertEqual(triage[group_key(self.ga)]['operator_review'],selection)
    def test_shared_metadata_does_not_share_quote_versions(self):
        x,y=self.proof(self.a),self.proof(self.b)
        for proof,a in [(x,self.a),(y,self.b)]:
            row=self.db.execute('SELECT url,state_id FROM sync_evidence WHERE evidence_id=?',(proof,)).fetchone()
            self.assertEqual(row[0],a['url']);self.assertEqual(sync.verify_quote(self.db,proof,current=True)['status'],'exact_literal_match')
        self.assertNotEqual(x,y)
    def test_native_guards_used_after_projection(self):
        with patch('engine.preflight',wraps=__import__('engine').preflight) as first,patch('engine.evidence',wraps=__import__('engine').evidence) as evidence,patch('engine.validate_saved_update',wraps=__import__('engine').validate_saved_update) as final:
            self.assertEqual(len(self.projection['work_groups']),1)
            selected=self.projection['work_groups'][0]['projected_group']['articles'][0]
            self.assertEqual(selected,self.a)
            result=self.adopt(selected,'adopt')
            self.assertEqual(result['status'],'accepted');self.assertGreater(first.call_count,0);self.assertGreater(evidence.call_count,0);self.assertGreater(final.call_count,0)
        self.assertEqual(pub.project(self.db,{})['events'],[])
    def test_same_progress_from_two_urls_is_counted_once(self):
        first=self.adopt(self.a,'first');self.assertEqual(first['status'],'accepted')
        count=events(self.db)[0]['progress_count'];self.assertEqual(count,1)
        second=self.adopt(self.b,'second');self.assertEqual(second['status'],'accepted')
        self.assertEqual(events(self.db)[0]['progress_count'],count);self.assertEqual(len(events(self.db)[0]['progress_history']),1)
    def test_revision_invalidates_all_actual_dependencies_and_approval(self):
        a=self.adopt(self.a,'first');b=self.adopt(self.b,'second');proof=self.proof(self.a)
        current=pub.target(events(self.db)[0],'article',self.a['url'])
        approval=pub.approve(self.db,'approval','e1','article',self.a['url'],expected_hash=digest(current),actor='simulation:operator',at=NOW,evidence_ids=[proof],decision_ids=[a['decision_id']],basis='SYNTHETIC title check')['approval_id']
        # B's native guard actually read A as existing-event context. This is
        # a real dependency, not an accidental representative substitution.
        dependent=self.db.execute('SELECT e.url FROM sync_dependencies d JOIN sync_evidence e USING(evidence_id) WHERE d.decision_id=?',(b['decision_id'],)).fetchall()
        self.assertIn((self.a['url'],),dependent)
        amended={**self.a,'title':self.a['title']+' SYNTHETIC 訂正'}
        sync.change_source(self.db,'amend',amended,sync.head(self.db,self.a['url'])['state_id'],NOW,body_text='SYNTHETIC '+amended['title'])
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(a['decision_id'],)).fetchone()[0],'needs_recheck')
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(b['decision_id'],)).fetchone()[0],'needs_recheck')
        self.assertEqual(self.db.execute('SELECT status FROM pub_state WHERE approval_id=?',(approval,)).fetchone()[0],'needs_recheck')
        self.assertFalse(pre.is_current(self.projection,[{**self.ga,'articles':[amended]},self.gb]))
    def test_external_body_revision_keeps_metadata_policy_but_invalidates_native_proofs(self):
        proof=sync.add_evidence(self.db,'e1',self.a['url'],'body','SYNTHETIC')
        result=self.adopt(self.a,'body-adoption',evidence=[proof])
        self.assertEqual(result['status'],'accepted')
        target=pub.target(events(self.db)[0],'field','current_stage')
        aid=pub.approve(self.db,'body-approval','e1','field','current_stage',expected_hash=digest(target),actor='simulation:operator',at=NOW,evidence_ids=[proof],decision_ids=[result['decision_id']],basis='SYNTHETIC body check')['approval_id']
        sync.change_source(self.db,'body-revision',self.a,sync.head(self.db,self.a['url'])['state_id'],NOW,body_text='SYNTHETIC corrected original')
        # Metadata representation alone cannot certify external body freshness.
        self.assertTrue(pre.is_current(self.projection,self.groups))
        self.assertFalse(sync.job_current(self.db,self.jobs['body-adoption']))
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(result['decision_id'],)).fetchone()[0],'needs_recheck')
        self.assertEqual(self.db.execute('SELECT status FROM pub_state WHERE approval_id=?',(aid,)).fetchone()[0],'needs_recheck')
    def test_operator_hold_cannot_be_overridden_by_alias(self):
        proof=self.proof(self.a)
        op=sync.record_reference(self.db,'hold','e1','operator','hold',{'basis':'SYNTHETIC pending operator'},[proof],NOW)
        self.assertEqual(self.adopt(self.a,'blocked')['status'],'operator_reconfirmation_required')
        self.assertEqual(self.db.execute('SELECT payload FROM sync_decisions WHERE decision_id=?',(op['decision_id'],)).fetchone()[0],dumps({'basis':'SYNTHETIC pending operator'}))
    def test_undo_projection_restores_without_reversing_adopted_history(self):
        adopted=self.adopt(self.a,'first');state=events(self.db)
        undo=pre.plan(self.groups,disabled_cohorts=[self.projection['cohorts'][0]['cohort_id']])
        self.assertEqual(pre.restore(undo),self.groups);self.assertEqual(events(self.db),state)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
        self.assertEqual(self.adopt(self.b,'after-undo')['status'],'accepted')
        self.assertEqual(events(self.db)[0]['progress_count'],1)
    def test_changed_job_cannot_reuse_old_operation(self):
        self.adopt(self.a,'first');before=events(self.db)
        with self.assertRaises(ValueError):self.adopt(self.a,'first')
        self.assertEqual(events(self.db),before)
    def test_unrelated_event_reference_stays_current(self):
        other=copy.deepcopy(self.e);other['event_id']='e2';other['articles'][0]['url']='https://example.test/unrelated'
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',('e2',dumps(other)))
        source=other['articles'][0];sync.seed(self.db,source)
        proof=sync.add_evidence(self.db,'e2',source['url'],'title',source['title'])
        op=sync.record_reference(self.db,'unrelated','e2','operator','same_event',{'basis':'SYNTHETIC unrelated'},[proof],NOW)
        self.adopt(self.a,'first');updated={**self.a,'title':self.a['title']+' SYNTHETIC 訂正'}
        sync.change_source(self.db,'change',updated,sync.head(self.db,self.a['url'])['state_id'],NOW,body_text='SYNTHETIC revised')
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(op['decision_id'],)).fetchone()[0],'current')
    def test_duplicate_execution_does_not_double_adopt_or_charge(self):
        first=self.adopt(self.a,'first');budget=self.db.execute('SELECT * FROM budget').fetchall()
        second=sync.adopt(self.db,'first',self.jobs['first'],NOW);self.assertEqual(second,first)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1);self.assertEqual(self.db.execute('SELECT * FROM budget').fetchall(),budget)

if __name__=='__main__':unittest.main()
