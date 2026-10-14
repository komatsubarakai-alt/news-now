import hashlib
import json
import re
import unittest
import shutil
import subprocess
from pathlib import Path
from build_review import build

class ReviewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node is needed for the page-JavaScript test')
    def test_page_javascript_json_round_trip(self):
        here=Path(__file__).parent
        result=subprocess.run(['node',str(here/'review_dom.cjs'),str(here/'review.html')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_frozen_pairs_and_unverified_adoptions(self):
        root=Path(__file__).resolve().parents[2];path=Path(__file__).with_name('evaluation.json');before=path.read_bytes()
        html=build(root);payload=json.loads(re.search(r'id="snapshot" type="application/json">(.*?)</script>',html,re.S).group(1))
        dataset=json.loads(before);expected=[r for r in dataset['rows'] if r['label']=='manual_review']
        self.assertEqual(len(payload['rows']),15)
        self.assertEqual([r['sample_id'] for r in payload['rows']],[r['sample_id'] for r in expected])
        self.assertEqual(payload['dataset_sha256'],hashlib.sha256(before).hexdigest())
        for row,source in zip(payload['rows'],expected):
            for field in ['incoming_article','reference_article','incoming_quote','reference_quote','target_event_id','evidence_sha256']:
                self.assertEqual(row[field],source[field])
            self.assertIn('未確認',row['same_reason'])
        self.assertEqual(sum('採用されても正解未確認' in r['current_status'] for r in payload['rows']),2)
        self.assertEqual(before,path.read_bytes())
    def test_html_is_standalone_and_text_safe(self):
        root=Path(__file__).resolve().parents[2];html=build(root)
        self.assertNotIn('src="http',html);self.assertIn("connect-src 'none'",html)
        self.assertNotIn('innerHTML',html);self.assertNotIn('fetch(',html)
        payload=re.search(r'id="snapshot" type="application/json">(.*?)</script>',html,re.S).group(1)
        self.assertNotIn('<',payload);self.assertIn("record_scope:'review_only'",html)
