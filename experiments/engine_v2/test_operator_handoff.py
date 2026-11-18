import base64,gzip,hashlib,json,re,tempfile,unittest
from pathlib import Path
import operator_app as app
import operator_handoff as handoff

class HandoffTests(unittest.TestCase):
    def test_full_utf8_html_restored_and_parts_concatenate_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);data='<!doctype html><html lang="ja">架空データ・閲覧専用</html>'.encode();source=p/'original.html';source.write_bytes(data)
            result=handoff.build(source,p/'out');html=(p/'out/save-preview.html').read_text();encoded=json.loads('['+re.search(r'const CHUNKS=\[\n(.*?)\n\];',html,re.S).group(1)+']')
            self.assertEqual(gzip.decompress(base64.b64decode(''.join(encoded))),data);self.assertEqual(source.read_bytes(),data);self.assertEqual(result['source_sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(''.join((p/'out'/('save-preview-part-%02d.txt'%i)).read_text() for i in range(1,result['wrapper_parts']+1)),html)
            self.assertIn("connect-src 'none'",html);self.assertNotIn('fetch(',html)
    def test_repository_output_rejected(self):
        with self.assertRaises(ValueError):handoff.build('/tmp/not-needed.html',app.ROOT)
    def test_symlink_part_is_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'source.html').write_text('<html></html>');(p/'keep').write_text('keep');(p/'out').mkdir();(p/'out/save-preview-part-01.txt').symlink_to(p/'keep')
            with self.assertRaises(ValueError):handoff.build(p/'source.html',p/'out')
            self.assertEqual((p/'keep').read_text(),'keep');self.assertFalse((p/'out/save-preview.html').exists())

if __name__=='__main__':unittest.main()
