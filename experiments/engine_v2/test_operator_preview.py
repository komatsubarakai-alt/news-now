import json
import tempfile
import unittest
from pathlib import Path
import operator_app as app
import operator_preview as preview

class OperatorPreviewTests(unittest.TestCase):
    def test_demo_readonly_html_without_network_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'screen.html';result=preview.build(p,demo=True);html=p.read_text()
            self.assertFalse(result['can_publish']);self.assertIn("connect-src 'none'",html)
            self.assertIn('offline-read-only',html);self.assertIn('閲覧専用',html)
            self.assertNotIn('src="http',html);self.assertNotIn('__LOCAL_TOKEN__',html)
    def test_output_inside_repo_rejected_before_write(self):
        with self.assertRaises(ValueError):preview.build(app.ROOT/'operator-test.html',demo=True)
    def test_missing_input_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'screen.html'
            with self.assertRaises(ValueError):preview.build(p)
            self.assertFalse(p.exists())
    def test_output_inside_source_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):preview.build(Path(td)/'screen.html',snapshot_root=td)
    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'dest').write_text('unchanged');(p/'link').symlink_to(p/'dest')
            with self.assertRaises(ValueError):preview.build(p/'link',demo=True)
            self.assertEqual((p/'dest').read_text(),'unchanged')
    def test_imported_snapshot_unapproved_and_html_injection_escaped(self):
        from operator_demo import populate
        db=app.connect();app.initialize(db);populate(db);event=app.events(db)[0];db.close()
        event['event_name']='</script><script>alert("x")</script>'
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'input';root.mkdir();(root/'events.json').write_text(json.dumps([event]))
            out=Path(td)/'screen.html';result=preview.build(out,snapshot_root=root);html=out.read_text()
            self.assertEqual(result['events'],1);self.assertNotIn('</script><script>alert',html)
            self.assertIn('\\u003c/script>',html);self.assertIn('"approved_preview": []',html)

if __name__=='__main__':unittest.main()
