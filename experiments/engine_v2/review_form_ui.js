/* No fetch, XHR, beacon, localStorage or automatic Dropbox upload. */
(function(){
'use strict';const R=ReviewRecords,manifest=JSON.parse(document.getElementById('reviewManifest').textContent),store=new R.ReviewStore(manifest);
const labels={identity:'追加記事の同一性',contradiction:'既存記事との矛盾',impact:'過去の判断への影響',evidence:'引用・根拠の再確認'};
const names={unconfirmed:'未確認',same:'同じ事件',different:'別の事件',unknown:'判断できない',yes:'あり',no:'なし',needed:'必要',unneeded:'不要'};
const forms=new Map();let pending=null,output='',unexported=false;
const node=(tag,text)=>{const n=document.createElement(tag);if(text)n.textContent=text;return n;};
const notice=text=>document.getElementById('notice').textContent=text;
function invalidatePending(){pending=null;document.getElementById('importPanel').hidden=true;}
function resetOutput(){output='';const b=document.getElementById('savedAck');b.disabled=true;b.textContent='JSONを保存したことを確認';document.getElementById('savedAckHelp').textContent='入力や復元結果が変わったため、もう一度JSONを書き出してください。このボタンは読み込みには不要です。';}
function outputReady(){const b=document.getElementById('savedAck');b.disabled=false;b.textContent='JSONを保存したことを確認';document.getElementById('savedAckHelp').textContent='JSONを作成しました。端末への保存やコピー先の保存を確認してから押してください。作成だけでは保存完了ではありません。';}
function revealTransfer(){document.getElementById('transfer').scrollIntoView({block:'nearest'});}
function summary(id){const value=store.current(id),head=store.head(id),n=Object.values(value.answers).filter(v=>v!=='unconfirmed').length;return n+'/4項目に回答（承認ではありません）・'+(store.dirty(id)?'未出力の変更あり':head?'記録あり':'未記録')+(head?' ／ 記録日時 '+new Date(head.recorded_at).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'})+' 日本時間':'');}
function refresh(){for(const [id,f] of forms){const data=store.current(id);for(const key in labels)f.controls[key].value=data.answers[key];f.memo.value=data.memo;f.status.textContent=summary(id);}}
for(const target of manifest.targets){
 const section=target.scope==='event'?Array.from(document.querySelectorAll('[data-review-event]')).find(n=>n.dataset.reviewEvent===target.event_id):Array.from(document.querySelectorAll('[data-review-judgment]')).find(n=>n.dataset.reviewJudgment===target.judgment_id);
 if(!section)throw Error('対象の表示位置がありません');
 const box=node('div');box.className='review-form';box.dataset.targetId=target.target_id;
 box.append(node('h3',target.scope==='event'?'事件全体の確認結果':'この判断だけの確認結果'));box.append(node('p',target.scope==='event'?'個別の判断は自動承認されません。':'事件全体の回答とは独立した記録です。'));
 const controls={};for(const key in labels){const label=node('label',labels[key]),select=node('select');select.setAttribute('aria-label',labels[key]);select.dataset.answer=key;for(const value of R.CHOICES[key]){const option=node('option',names[value]);option.value=value;if(key==='contradiction'&&['yes','no'].includes(value))option.textContent=value==='yes'?'矛盾あり':'矛盾なし';if(key==='impact'&&['yes','no'].includes(value))option.textContent=value==='yes'?'影響あり':'影響なし';select.append(option);}label.append(select);box.append(label);controls[key]=select;}
 const label=node('label','確認メモ（任意）'),memo=node('textarea');memo.maxLength=10000;memo.setAttribute('aria-label','確認メモ');label.append(memo);box.append(label);const status=node('p');status.className='record-status';box.append(status);
 const history=node('details'),heading=node('summary','この確認結果の履歴');history.append(heading);const historyText=node('pre');history.append(historyText);history.addEventListener('toggle',()=>{if(history.open){const rows=store.history.filter(r=>r.binding.target_id===target.target_id).map(r=>({record_id:r.record_id,previous_record_id:r.previous_record_id,answers:r.answers,memo:r.memo,recorded_at:r.recorded_at,reviewed_at:r.reviewed_at}));historyText.textContent=rows.length?JSON.stringify(rows,null,2):'未記録';}});box.append(history);
 function change(){try{store.edit(target.target_id,{answers:Object.fromEntries(Object.entries(controls).map(([k,s])=>[k,s.value])),memo:memo.value});unexported=true;resetOutput();invalidatePending();status.textContent=summary(target.target_id);notice('入力を変更しました。端末ファイルには未保存です。JSONを書き出してください。');}catch(e){notice('入力エラー：'+e.message);}}
 box.addEventListener('change',change);memo.addEventListener('input',change);forms.set(target.target_id,{controls,memo,status});
 if(target.scope==='event')section.insertBefore(box,Array.from(section.children).find(n=>n.tagName==='H3'&&n.textContent==='各判断への影響')||null);else section.append(box);
}
refresh();
function prepareExport(){output=store.exportJSON();document.getElementById('jsonTransfer').value=output;unexported=true;outputReady();invalidatePending();refresh();return output;}
document.getElementById('exportJson').addEventListener('click',async()=>{
 try{const text=prepareExport(),fileName='news-now-review-results-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';
  if(window.showSaveFilePicker){try{const handle=await window.showSaveFilePicker({suggestedName:fileName,types:[{description:'確認結果JSON',accept:{'application/json':['.json']}}]});const writer=await handle.createWritable();await writer.write(text);await writer.close();unexported=false;document.getElementById('savedAckHelp').textContent='ファイルへの書き込みが完了しました。保存先を確認したうえで確認ボタンを押せます。';notice('指定したファイルへの書き込みが完了しました。Dropboxへは送信していません。');return;}catch(e){if(e.name==='AbortError'){notice('保存を取り消しました。出力JSONはコピー欄に残っています。');return;}notice('直接保存できません。ダウンロードかJSONコピーを利用してください。');}}
  const url=URL.createObjectURL(new Blob([text],{type:'application/json'})),link=node('a');link.href=url;link.download=fileName;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notice('JSON出力を開始しました。実際に保存されたかダウンロード先で確認してください。保存できない場合は下の「JSONのコピー・読み込み」欄を使用してください。');
 }catch(e){revealTransfer();notice('出力・保存に失敗しました：'+e.message+'。今回の入力はファイルに保存されていません。');}
});
document.getElementById('copyJson').addEventListener('click',async()=>{let text;try{text=prepareExport();}catch(e){notice('JSON生成失敗：'+e.message+'。今回の入力は未保存です。');return;}revealTransfer();try{if(!navigator.clipboard?.writeText)throw Error('自動コピー不可');await navigator.clipboard.writeText(text);notice('JSONをコピーしました。非公開のファイルへ貼り付けて保存してください。');}catch(e){const field=document.getElementById('jsonTransfer');field.focus();field.select();notice('自動コピーできません。選択したJSONを長押ししてコピーしてください。');}});
function validate(text){invalidatePending();try{pending=store.planImport(text);document.getElementById('importSummary').textContent='検証成功：新規・更新 '+pending.added+'記録、古い記録を維持 '+pending.older+'対象。元の判断・承認・本番データは変更しません。';document.getElementById('importPanel').hidden=false;notice('まだ取り込んでいません。内容を確認して取り込みボタンを押してください。');}catch(e){notice('取り込み拒否：'+e.message+'。現在の入力は変更していません。');}}
document.getElementById('validateJson').addEventListener('click',()=>validate(document.getElementById('jsonTransfer').value));
document.getElementById('jsonTransfer').addEventListener('input',invalidatePending);
async function readFile(file){if(typeof file.text==='function')return file.text();if(typeof FileReader==='undefined')throw Error('ファイル読み取りに非対応です');return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('ファイルを読み取れません'));reader.onabort=()=>reject(Error('ファイル読み取りが中断されました'));reader.readAsText(file,'UTF-8');});}
document.getElementById('importFile').addEventListener('change',async e=>{invalidatePending();try{const f=e.target.files[0];if(!f)return;if(f.size>5000000)throw Error('5MBを超えています');validate(await readFile(f));}catch(err){notice('読み込み失敗：'+err.message+'。JSON全文を上の欄に貼り付けて検証してください。現在の入力は変更していません。');}finally{e.target.value='';}});
document.getElementById('applyImport').addEventListener('click',()=>{if(!pending)return;try{const result=store.applyImport(pending);if(result.added)resetOutput();invalidatePending();refresh();notice('取り込み完了：'+result.added+'記録。確認結果のみ復元しました。元の判断・承認・本番データは変更していません。');}catch(e){invalidatePending();notice('取り込み拒否：'+e.message);}});
document.getElementById('cancelImport').addEventListener('click',()=>{invalidatePending();notice('取り込みを取り消しました。');});
document.getElementById('savedAck').addEventListener('click',()=>{if(!output)return;unexported=false;document.getElementById('savedAck').textContent='保存確認済み（再確認できます）';document.getElementById('savedAckHelp').textContent='運営者が保存先を確認したことだけを記録しました。回答・承認・本番データは変わりません。';notice('端末への保存を確認したことを記録しました。元の判断・承認・本番データは変更していません。');});
window.addEventListener('beforeunload',e=>{if(unexported){e.preventDefault();e.returnValue='';}});
// Memory-only instance is exposed for offline regression verification, never a server API.
window.reviewFormStore=store;
})();
