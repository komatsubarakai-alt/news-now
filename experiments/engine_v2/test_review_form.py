"""Fictional-only generator/record tests. Node tests the same embedded browser core."""
import copy,json,os,subprocess,tempfile,unittest
from pathlib import Path
import operator_applicability as a
from operator_applicability_demo import examples
import review_form as f

class ReviewFormTests(unittest.TestCase):
    def setUp(self):
        all_rows=a.evaluate(examples());self.result={**all_rows,'assessments':[r for r in all_rows['assessments'] if r['state']=='recheck']};self.m=f.manifest(self.result)
    def test_four_events_twenty_judgments(self):
        self.assertEqual(len(self.m['targets']),24);self.assertEqual(sum(t['scope']=='event' for t in self.m['targets']),4)
    def test_originals_unchanged(self):
        before=copy.deepcopy(self.result);f.render(self.result);self.assertEqual(before,self.result)
    def test_manifest_version_binding(self):
        for t in self.m['targets']:
            self.assertIn('judgment_hash',t);self.assertIn('current_hash',t)
            for article in t['article_versions']['target_current']:self.assertTrue(article['article_version_id']);self.assertEqual(article['version_origin'],'derived_saved_metadata')
    def test_fake_derived_not_historic_version(self):
        self.assertIn('正式版ID',f.render(self.result));self.assertIn('derived:',f.render(self.result))
    def test_original_decision_corruption_rejected(self):
        self.result['assessments'][0]['original_decision']['note']='changed'
        with self.assertRaises(ValueError):f.manifest(self.result)
    def test_current_article_corruption_rejected(self):
        self.result['assessments'][0]['current_target']['articles'][0]['title']='changed'
        with self.assertRaises(ValueError):f.manifest(self.result)
    def test_duplicate_judgment_rejected(self):
        self.result['assessments'].append(self.result['assessments'][0])
        with self.assertRaises(ValueError):f.manifest(self.result)
    def test_source_article_corruption_rejected(self):
        self.result['assessments'][0]['source_group']['articles'][0]['description']='changed'
        with self.assertRaises(ValueError):f.manifest(self.result)
    def test_standalone_and_no_network_writer(self):
        text=f.render(self.result)
        for forbidden in ('<script src=','<link ','fetch(','XMLHttpRequest(','sendBeacon(','localStorage.'):
            self.assertNotIn(forbidden,text)
        self.assertIn("connect-src 'none'",text);self.assertTrue('JSONへ書き出す' in text);self.assertIn('JSONを保存したことを確認',text)
    def test_import_controls_not_collapsed(self):
        text=f.render(self.result)
        self.assertIn('<div id="transfer" role="region"',text)
        self.assertNotIn('<details id="transfer"',text)
        self.assertIn('placeholder="保存したJSON全文をここに貼り付けてください"',text)
        self.assertIn('貼り付けたJSONを検証する',text)
    def test_file_picker_not_mime_restricted(self):
        text=f.render(self.result)
        self.assertIn('<input id="importFile" type="file" hidden>',text)
        self.assertIn('reader.readAsText',text)
    def test_ack_explains_when_it_is_enabled(self):
        text=f.render(self.result)
        self.assertIn('aria-describedby="savedAckHelp"',text)
        self.assertIn('読み込みのために押す必要はありません',text)
        ui=(f.HERE/'review_form_ui.js').read_text()
        self.assertNotIn("await writer.close();unexported=false;document.getElementById('savedAck').disabled=true",ui)
        self.assertIn('保存確認済み（再確認できます）',ui)
    def test_import_stage_displays_exist(self):
        text=f.render(self.result)
        for identifier in ('selectedFileName','readStatus','verifyStatus','restoreStatus','restoreComparisonSummary'):
            self.assertIn('id="'+identifier+'"',text)
    def test_file_import_precedes_auxiliary_paste(self):
        text=f.render(self.result)
        self.assertLess(text.index('id="chooseImportFile"'),text.index('id="jsonTransfer"'))
        self.assertIn('長いJSONを貼り付ける必要はありません',text)
    def test_restore_comparison_is_not_approval(self):
        text=f.render(self.result)
        self.assertIn('復元した回答・メモの照合',text)
        self.assertIn('元判断の正しさや承認を確認した結果ではありません',text)
    def test_private_output_outside_repo(self):
        with self.assertRaises(ValueError):f.write(self.result,a.ROOT/'private.html')
    def test_schema_js_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'manifest.json';p.write_text(a.canonical(self.m));r=subprocess.run(['node',str(f.HERE/'review_form_tests.cjs'),str(p)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr);self.assertIn('"failed":0',r.stdout)

if __name__=='__main__':unittest.main()
