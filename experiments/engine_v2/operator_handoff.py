"""Offline HTML download wrapper with gzip + SHA-256; no hosting or requests."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import operator_app as app


def build(source, output_dir):
    source=Path(source);out=Path(output_dir)
    if out.is_symlink() or out.resolve().is_relative_to(app.ROOT):raise ValueError('handoff must be outside repository')
    if not source.is_file():raise ValueError('source HTML required')
    data=source.read_bytes();encoded=base64.b64encode(gzip.compress(data,mtime=0)).decode('ascii');expected=hashlib.sha256(data).hexdigest()
    chunks=',\n'.join(json.dumps(encoded[i:i+1200]) for i in range(0,len(encoded),1200))
    html='''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>確認用HTMLを端末に保存</title><style>body{font:18px/1.7 system-ui,sans-serif;max-width:600px;margin:auto;padding:20px;background:#f1f5f3;color:#183432}button{font:inherit;min-height:52px;padding:12px;background:#176b56;color:white;border:0;border-radius:9px}code{overflow-wrap:anywhere}p{white-space:pre-wrap}</style>
<h1>ニュースの現在地</h1><p>確認用の閲覧専用HTMLを端末に保存します。インターネットへ送信しません。判断・承認は保存できません。</p>
<button id="save">閲覧用HTMLを保存</button><p id="status" role="status"></p><details><summary>ファイルの確認情報</summary><p>SHA-256：<code id="hash"></code></p></details>
<script>
const CHUNKS=[
''' +chunks+'''
];
const EXPECTED='''+json.dumps(expected)+''';
document.getElementById('hash').textContent=EXPECTED;
document.getElementById('save').onclick=async()=>{
 const button=document.getElementById('save'),status=document.getElementById('status');button.disabled=true;
 try{
  if(!globalThis.DecompressionStream||!globalThis.crypto?.subtle)throw Error('このブラウザーでは復元・照合機能が利用できません。新しいChromeなどで端末内のHTMLファイルとして開いてください。');
  const compressed=Uint8Array.from(atob(CHUNKS.join('')),c=>c.charCodeAt(0));
  const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
  const bytes=new Uint8Array(await new Response(stream).arrayBuffer());
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
  if(hash!==EXPECTED)throw Error('内容の照合が一致しません。安全のため保存を停止しました。');
  const url=URL.createObjectURL(new Blob([bytes],{type:'text/html;charset=utf-8'})),a=document.createElement('a');
  a.href=url;a.download='news-now-operator-simple-preview.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
  status.textContent='内容の照合に成功しました。ダウンロードした news-now-operator-simple-preview.html を開いてください。';
 }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
};
</script></html>
'''
    out.mkdir(parents=True,exist_ok=True);target=out/'save-preview.html'
    if target.is_symlink():raise ValueError('symlink output forbidden')
    parts=[];part=''
    for line in html.splitlines(keepends=True):
        if part and len(part)+len(line)>3500:parts.append(part);part=''
        part+=line
    if part:parts.append(part)
    planned=[target,out/'handoff-manifest.json']+[out/('save-preview-part-%02d.txt'%i) for i in range(1,len(parts)+1)]
    if any(p.is_symlink() or p.resolve().is_relative_to(app.ROOT) or p.resolve()==source.resolve() for p in planned):raise ValueError('unsafe handoff output')
    target.write_text(html,encoding='utf-8')
    for index,value in enumerate(parts,1):(out/('save-preview-part-%02d.txt'%index)).write_text(value,encoding='utf-8')
    assert ''.join(parts)==html
    result=dict(scope='local_offline_readonly_handoff',source_bytes=len(data),source_sha256=expected,wrapper_bytes=len(html.encode()),wrapper_sha256=hashlib.sha256(html.encode()).hexdigest(),wrapper_parts=len(parts),network_requests_required=0,filename=target.name)
    (out/'handoff-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');return result

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--html',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args();print(json.dumps(build(a.html,a.output_dir),ensure_ascii=False))
