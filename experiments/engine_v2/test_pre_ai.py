"""Fictional-only regression cases. No real news, network or production writes."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import pre_ai as p

NOW=datetime(2026,1,1,tzinfo=timezone.utc)
def article(url='a',title='架空リーグ 試合結果 勝利',**kw):
    return {'url':'https://example.test/'+url,'title':title,'description':'',
            'published':'2026-01-01T00:00:00Z','rss_source':{'name':'架空配信元'},
            'collected_at':'2026-01-01T01:00:00Z',**kw}
def group(a,**kw):
    return {'event_name':a['title'],'articles':[a],'article_count':1,'source_article_count':1,**kw}

class ScopeTests(unittest.TestCase):
    def check(self,title,status):self.assertEqual(p.classify(article(title=title))['status'],status)
    def test_sports_result(self):self.check('架空J2リーグ 試合結果 勝利','outside_candidate')
    def test_product_context(self):self.check('架空店 新作ケーキ 限定販売','outside_candidate')
    def test_event_context(self):self.check('架空写真展 開催中 20日まで','outside_candidate')
    def test_celebrity_context(self):self.check('藤井風 カフェでのショット','outside_candidate')
    def test_keyword_alone(self):self.check('架空写真展','hold')
    def test_profile_without_tracking_issue_held(self):self.check('＜ひと＞架空選手 マラソンで優勝','hold')
    def test_missing_title(self):self.check('','hold')
    def test_product_accident_protected(self):self.check('新作ケーキ 食中毒で販売中止','inside')
    def test_sport_misconduct_protected(self):self.check('架空リーグ 不祥事 捜査開始','inside')
    def test_facility_protected(self):self.check('架空アリーナ 建設計画','inside')
    def test_policy_protected(self):self.check('架空大会 条例改正による制度変更','inside')
    def test_disaster_protected(self):self.check('架空市 大雨警報 浸水被害','inside')
    def test_corporate_protected(self):self.check('架空会社 TOB成立 上場廃止','inside')
    def test_rss_protection_wins(self):
        a=article(description='<p>試合会場の重大事故で捜査</p>');self.assertEqual(p.classify(a)['status'],'inside')
    def test_missing_location_not_difference(self):self.check('架空ニュースの続き','hold')
    def test_environment_sports_held(self):self.check('スポGOMI大会 優勝','hold')
    def test_ambiguous_school_event(self):self.check('架空学校の写真展 開催','hold')
    def test_no_source_confirmation_invented(self):
        r=p.classify(article());self.assertFalse(r['safe_exclusion_confirmed']);self.assertFalse(r['body_verified'])
    def test_revision_rechecked(self):
        a=article();b={**a,'description':'訂正。会場で事故'}
        self.assertNotEqual(p.classify(a)['article_version'],p.classify(b)['article_version']);self.assertEqual(p.classify(b)['status'],'inside')

class RepresentationTests(unittest.TestCase):
    def setUp(self):self.a=article(title='架空ニュース');self.b=article('b',title='架空ニュース')
    def plan(self,**kw):return p.plan([group(self.a),group(self.b)],**kw)
    def test_exact_metadata_alias_without_body(self):
        r=self.plan();self.assertEqual(len(r['cohorts']),1);self.assertTrue(r['cohorts'][0]['active']);self.assertEqual(len(r['work_groups']),1)
        self.assertFalse(r['cohorts'][0]['body_identity_proved']);self.assertFalse(r['can_publish'])
    def test_all_original_metadata_retained(self):
        r=self.plan();self.assertEqual(p.restore(r),[group(self.a),group(self.b)]);self.assertEqual(len(r['articles']),2)
    def test_deterministic_reordering(self):
        r=self.plan();s=p.plan([group(self.b),group(self.a)])
        self.assertEqual(r['aliases'],s['aliases']);self.assertEqual(r['cohorts'],s['cohorts'])
    def test_title_no_normalization(self):
        self.b['title']='架空ニュース ';self.assertEqual(len(self.plan()['cohorts']),0)
    def test_full_title_suffix_is_not_stripped(self):
        self.b['title']+=' - 架空媒体';self.assertFalse(self.plan()['cohorts'])
    def test_source_no_normalization(self):
        self.b['rss_source']['name']+=' ';self.assertFalse(self.plan()['cohorts'])
    def test_unicode_no_normalization(self):
        self.a['title']='Ａ';self.b['title']='A';self.assertFalse(self.plan()['cohorts'])
    def test_jst_day_not_utc_day(self):
        self.a['published']='2025-12-31T16:00:00Z';self.b['published']='2026-01-01T10:00:00Z';self.assertTrue(self.plan()['cohorts'])
    def test_different_jst_day(self):
        self.b['published']='2026-01-01T16:00:00Z';self.assertFalse(self.plan()['cohorts'])
    def test_rfc_rss_datetime(self):self.assertEqual(p.jst_day('Wed, 31 Dec 2025 16:00:00 GMT'),'2026-01-01')
    def test_missing_date(self):
        self.b.pop('published');self.assertFalse(self.plan()['cohorts'])
    def test_naive_date(self):
        self.b['published']='2026-01-01T01:00:00';self.assertFalse(self.plan()['cohorts'])
    def test_missing_source(self):
        self.b.pop('rss_source');self.assertFalse(self.plan()['cohorts'])
    def test_unknown_source(self):
        self.b['rss_source']['name']='不明';self.assertFalse(self.plan()['cohorts'])
    def test_known_distinct(self):
        r=self.plan(separate_pairs=[[p.version_id(self.a),p.version_id(self.b)]]);self.assertFalse(r['cohorts'][0]['active']);self.assertEqual(len(r['work_groups']),2)
    def test_different_saved_event_bindings(self):
        r=self.plan(event_bindings={p.version_id(self.a):['e1'],p.version_id(self.b):['e2']});self.assertFalse(r['cohorts'][0]['active'])
    def test_group_event_ids_block(self):
        r=p.plan([group(self.a,event_id='e1'),group(self.b,event_id='e2')]);self.assertFalse(r['cohorts'][0]['active'])
    def test_shared_event_bindings_preserved(self):
        bindings={p.version_id(a):['e1','e2'] for a in [self.a,self.b]};before=copy.deepcopy(bindings)
        r=self.plan(event_bindings=bindings);self.assertTrue(r['cohorts'][0]['active']);self.assertEqual(bindings,before)
    def test_same_url_revisions_never_collapse(self):
        old={**self.a,'description':'旧版'};r=p.plan([group(old),group(self.a),group(self.b)])
        self.assertEqual(len(r['work_groups']),3);self.assertFalse(r['cohorts'])
    def test_metadata_only_occurrence_not_revision(self):
        more={**self.a,'collected_at':'2026-01-02T00:00:00Z'};r=p.plan([group(self.a),group(more)])
        self.assertEqual(len(r['articles']),1);self.assertEqual(len(r['occurrences'][p.version_id(self.a)]),2)
    def test_conflicting_source_metadata(self):
        other={**self.a,'rss_source':{'name':'別配信元'}};r=p.plan([group(self.a),group(other),group(self.b)]);self.assertFalse(r['cohorts'])
    def test_representative_changed_new_receipt(self):
        old=self.plan();self.a['title']='改訂された架空ニュース';r=self.plan();self.assertEqual(len(r['work_groups']),2);self.assertEqual(len(old['cohorts']),1)
    def test_body_change_invalidates(self):self.assertNotEqual(p.version_id(self.a),p.version_id({**self.a,'body':'改訂'}))
    def test_disable_and_restore(self):
        old=self.plan();r=self.plan(disabled_cohorts=[old['cohorts'][0]['cohort_id']]);self.assertFalse(r['cohorts'][0]['active']);self.assertEqual(len(r['work_groups']),2)
    def test_same_url_same_revision_not_cross_url_saving(self):
        r=p.plan([group(self.a),group(self.a)]);self.assertFalse(r['cohorts']);self.assertEqual(len(r['articles']),1)
    def test_different_group_context_not_collapsed(self):
        r=p.plan([group(self.a,judgment_id='j1'),group(self.b,judgment_id='j2')]);self.assertEqual(len(r['work_groups']),2)
    def test_outside_scope_aliases_still_preserved(self):
        self.a['title']=self.b['title']='架空写真展 開催中';r=self.plan();self.assertEqual(len(r['work_groups']),0)
        self.assertTrue(r['cohorts'][0]['active']);self.assertEqual(len(r['deferred_groups']),2);self.assertEqual(len(p.restore(r)),2)
    def test_mixed_group_keeps_protected(self):
        b=article('b',title='架空市 火災');a=article();r=p.plan([{'event_name':'架空複合記事','articles':[a,b]}]);self.assertEqual(len(r['work_groups'][0]['projected_group']['articles']),1);self.assertEqual(len(r['articles']),2)
    def test_operator_override_version_bound(self):
        a=article();r=p.plan([group(a)],overrides={p.version_id(a):{'status':'inside','record_id':'op'}});self.assertEqual(len(r['work_groups']),1)
        with self.assertRaises(ValueError):p.plan([group({**a,'description':'改訂'})],overrides={p.version_id(a):{'status':'inside','record_id':'op'}})
    def test_receipt_freshness_after_revision(self):
        r=self.plan();self.assertTrue(p.is_current(r,[group(self.a),group(self.b)]))
        self.a['description']='記事改訂';self.assertFalse(p.is_current(r,[group(self.a),group(self.b)]))
    def test_receipt_freshness_after_event_binding_change(self):
        r=self.plan();self.assertFalse(p.is_current(r,[group(self.a),group(self.b)],event_bindings={p.version_id(self.a):['e1']}))
    def test_existing_event_candidate_is_not_deferred(self):
        a=article();r=p.plan([group(a,event_id='e1')]);self.assertEqual(r['scope'][p.version_id(a)]['status'],'hold');self.assertEqual(len(r['work_groups']),1)
    def test_multi_article_context_is_not_group_merge(self):
        unrelated=article('c',title='架空の別記事')
        r=p.plan([{'event_name':'別グループ','articles':[self.a,unrelated]},group(self.b)])
        self.assertEqual(len(r['work_groups']),2);self.assertEqual(len(p.restore(r)[0]['articles']),2)
    def test_known_separate_transitive_cohort_is_all_blocked(self):
        third=article('c',title=self.a['title'])
        r=p.plan([group(self.a),group(self.b),group(third)],separate_pairs=[[p.version_id(self.a),p.version_id(third)]])
        self.assertFalse(r['cohorts'][0]['active']);self.assertEqual(len(r['work_groups']),3)
    def test_no_input_mutation(self):
        g=[group(self.a),group(self.b)];before=copy.deepcopy(g);p.plan(g);self.assertEqual(g,before)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'private.sqlite');self.db=p.open_store(self.path);self.groups=[group(article())]
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def save(self,**kw):return p.save_plan(self.db,'op',self.groups,NOW,**kw)
    def test_restart_replay(self):
        r=self.save();self.db.close();self.db=p.open_store(self.path);self.assertEqual(self.save(),r);self.assertEqual(self.db.execute('SELECT count(*) FROM pre_ai_receipts').fetchone()[0],1)
    def test_failure_rollback(self):
        with self.assertRaises(RuntimeError):self.save(fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pre_ai_receipts').fetchone()[0],0);self.save()
    def test_changed_inputs_same_operation_rejected(self):
        self.save();self.groups[0]['articles'][0]['description']='更新'
        with self.assertRaises(ValueError):self.save()
    def test_new_operation_preserves_old(self):
        self.save();self.groups[0]['articles'][0]['description']='訂正';p.save_plan(self.db,'new',self.groups,NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pre_ai_receipts').fetchone()[0],2)
    def test_rule_change_invalidates_replay(self):
        self.save()
        with patch.object(p,'rule_id',return_value='new-rule'):
            with self.assertRaises(ValueError):self.save()
    def test_no_merge_progress_publication_or_budget_tables_touched(self):
        for name in ['events','decisions','budget','approvals','facts']:
            self.db.execute(f'CREATE TABLE {name}(payload TEXT)');self.db.execute(f'INSERT INTO {name} VALUES(?)',('fictional unchanged',))
        self.save()
        for name in ['events','decisions','budget','approvals','facts']:
            self.assertEqual(self.db.execute(f'SELECT * FROM {name}').fetchall(),[('fictional unchanged',)])
    def test_nested_savepoint_does_not_commit_caller(self):
        self.db.execute('BEGIN');self.save();self.db.rollback();self.assertEqual(self.db.execute('SELECT count(*) FROM pre_ai_receipts').fetchone()[0],0)
    def test_readonly_store_fails_without_success(self):
        self.db.close();self.db=sqlite3.connect(f'file:{self.path}?mode=ro',uri=True)
        with self.assertRaises(sqlite3.OperationalError):self.save()
    def test_no_network_api_import_or_call(self):
        with patch('socket.socket.connect',side_effect=AssertionError('forbidden')),patch('urllib.request.urlopen',side_effect=AssertionError('forbidden')):
            r=self.save();self.assertEqual(r['ai_calls'],0);self.assertFalse(r['dispatch_enabled'])

if __name__=='__main__':unittest.main()
