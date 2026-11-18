import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from operator_revalidation import validate_records,cohort,diagnose,run,summarize

HERE=Path(__file__).parent

class OperatorRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.raw=(HERE/'operator_review_records.json').read_bytes()
        self.dataset_raw=(HERE/'evaluation.json').read_bytes()
        self.review=json.loads(self.raw);self.dataset=json.loads(self.dataset_raw)
        self.rows={r['sample_id']:r for r in self.dataset['rows']}
    def encode(self,data):return json.dumps(data,ensure_ascii=False).encode()
    def test_exact_attachment_integrity_and_counts(self):
        dataset,records=validate_records(self.raw,self.dataset_raw)
        self.assertEqual(len(records),15)
        self.assertEqual(sum(r['choice']=='same_event' for r in records.values()),13)
        self.assertEqual(sum(r['choice']=='hold' for r in records.values()),2)
        self.assertEqual(sum(r['incoming_body_checked'] and r['reference_body_checked'] for r in records.values()),7)
        self.assertEqual(dataset,self.dataset)
    def test_invalid_records_rejected_atomically(self):
        changes=[lambda d:d.update(dataset_sha256='stale'),lambda d:d.update(snapshot_revision='stale'),
            lambda d:d.update(repository='other/repo'),lambda d:d.update(schema='gold'),
            lambda d:d['records'].pop(),lambda d:d['records'].__setitem__(1,copy.deepcopy(d['records'][0])),
            lambda d:d['records'][0].update(event_id='other'),lambda d:d['records'][0].update(evidence_sha256='stale'),
            lambda d:d['records'][0].update(record_scope='gold'),lambda d:d['records'][0].update(choice='new_event'),
            lambda d:d['records'][0].update(incoming_body_checked='true'),lambda d:d['records'][0].update(reference_body_checked=1),
            lambda d:d['records'][0].update(reviewed_at='2026-10-15T00:00:00'),lambda d:d['records'][0].update(reviewed_at='bad'),
            lambda d:d['records'][0].update(note='x'*5001),lambda d:d['records'][0].update(sample_id='real-001')]
        for change in changes:
            data=copy.deepcopy(self.review);change(data)
            with self.subTest(data=data):
                with self.assertRaises(ValueError):validate_records(self.encode(data),self.dataset_raw)
        self.assertEqual((HERE/'operator_review_records.json').read_bytes(),self.raw)
    def test_duplicate_json_keys_and_tampered_evaluation_rejected(self):
        bad=self.raw.replace(b'"schema":',b'"schema":"invalid","schema":',1)
        with self.assertRaises(ValueError):validate_records(bad,self.dataset_raw)
        changed=copy.deepcopy(self.dataset);changed['rows'][18]['incoming_article']['title']='changed'
        raw=self.encode(changed);review=copy.deepcopy(self.review);review['dataset_sha256']=hashlib.sha256(raw).hexdigest()
        with self.assertRaises(ValueError):validate_records(self.encode(review),raw)
    def test_body_cohorts_never_promote_unverified_or_hold(self):
        _,records=validate_records(self.raw,self.dataset_raw)
        expected={'real-019':'operator_same_partial','real-020':'operator_hold_neither',
                  'real-021':'operator_hold_partial','real-022':'operator_same_neither','real-024':'operator_same_both'}
        for sid,name in expected.items():self.assertEqual(cohort(self.rows[sid],records[sid]),name)
        metrics=[]
        for sid,record in records.items():
            metrics.append({'cohort':cohort(self.rows[sid],record),'policy':'fixed4','target_position':1,
                'target_present':True,'total_input_bytes':10,'batches':1,'cpu_ms':1,'status':'needs_evidence',
                'input_preserved':True,'candidate_states_preserved':True})
        summary=summarize(metrics)
        self.assertEqual(summary['operator_same_both']['candidate_recall'],1)
        for name,value in summary.items():
            if name!='operator_same_both':self.assertIsNone(value['candidate_recall'])
    def test_deletion_missing_and_duplicate_article_are_different(self):
        _,records=validate_records(self.raw,self.dataset_raw)
        stored=json.loads((HERE.parents[1]/'events.json').read_text())
        output={sid:diagnose(self.rows[sid],record,stored) for sid,record in records.items()}
        self.assertEqual(output['real-020']['operator_reported_deleted'],['reference','incoming'])
        self.assertEqual(output['real-021']['operator_reported_body_missing'],['incoming'])
        self.assertEqual(output['real-022']['duplicate_class'],'exact_url_revision')
        self.assertTrue(output['real-022']['same_url_multiple_events'])
        self.assertEqual(len(output['real-022']['stored_url_event_links']),1)
        self.assertEqual(output['real-030']['duplicate_class'],'same_headline_time_different_url')
        self.assertEqual(output['real-033']['duplicate_class'],'similar_headline_different_url')
        self.assertFalse(output['real-033']['equal_fields']['published'])
        self.assertTrue(all(not d['collection_duplicate_confirmed'] and not d['body_text_supplied'] for d in output.values()))
    def test_replay_never_receives_review_notes_choices_or_body_flags(self):
        root=HERE.parents[1];before_dataset=self.dataset_raw;calls=[]
        def fake(root_arg,dataset,batch_size):
            self.assertEqual(dataset,self.dataset);calls.append(batch_size)
            rows=[]
            for row in dataset['rows']:
                rows.append({'sample_id':row['sample_id'],'target_event_id':row['target_event_id'],
                    'target_rank':1,'target_retained':True,'staged_total_bytes':10,'staged_batches':1,
                    'staged_ms':1,'staged_status':'needs_evidence','input_preserved':True,
                    'all_candidate_states_preserved':True,'legacy_total_bytes':20,'legacy_calls':1,
                    'legacy_ms':1,'legacy_status':'needs_evidence','legacy_input_preserved':True,'legacy_catalog_preserved':True})
            return {'rows':rows}
        with patch('operator_revalidation.evaluate',side_effect=fake):result=run(root,self.dataset_raw,self.raw)
        self.assertEqual(calls,[4,8,(4,64)])
        self.assertFalse(result['gold_dataset_modified']);self.assertFalse(result['production_decisions_written'])
        held=next(s for s in result['specimens'] if s['sample_id']=='real-021')
        self.assertTrue(held['target_excluded_by_existing_hold'])
        self.assertEqual((HERE/'evaluation.json').read_bytes(),before_dataset)
        self.assertTrue(result['integrity']['json_hashes_unchanged'])
        self.assertTrue(all(not s['gold_label_written'] for s in result['specimens']))
