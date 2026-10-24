/* Offline-only review records. No production writer, network client or approvals. */
(function(root){
'use strict';
const TYPE='news-now-private-review', VERSION=1;
const CHOICES={identity:['unconfirmed','same','different','unknown'],contradiction:['unconfirmed','yes','no','unknown'],impact:['unconfirmed','yes','no','unknown'],evidence:['unconfirmed','needed','unneeded','unknown']};
const clone=x=>JSON.parse(JSON.stringify(x));
function canonical(x){if(x===null||typeof x!=='object')return JSON.stringify(x);if(Array.isArray(x))return '['+x.map(canonical).join(',')+']';return '{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+canonical(x[k])).join(',')+'}';}
function sha256(text){
 const data=new TextEncoder().encode(text),length=data.length,total=Math.ceil((length+9)/64)*64,buf=new Uint8Array(total);buf.set(data);buf[length]=128;
 const view=new DataView(buf.buffer);view.setUint32(total-8,Math.floor(length/0x20000000));view.setUint32(total-4,(length*8)>>>0);
 const k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
 let h=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];const rr=(x,n)=>(x>>>n)|(x<<(32-n));
 for(let offset=0;offset<total;offset+=64){const w=new Uint32Array(64);for(let i=0;i<16;i++)w[i]=view.getUint32(offset+i*4);for(let i=16;i<64;i++){let x=w[i-15],y=w[i-2];w[i]=(w[i-16]+(rr(x,7)^rr(x,18)^(x>>>3))+w[i-7]+(rr(y,17)^rr(y,19)^(y>>>10)))>>>0;}let [a,b,c,d,e,f,g,z]=h;for(let i=0;i<64;i++){const t1=(z+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+k[i]+w[i])>>>0,t2=((rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c)))>>>0;z=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}h=h.map((x,i)=>(x+[a,b,c,d,e,f,g,z][i])>>>0);}
 return h.map(x=>x.toString(16).padStart(8,'0')).join('');
}
const digest=x=>sha256(canonical(x));
function fail(message){throw Error(message);}
function keys(x,expected){if(!x||typeof x!=='object'||Array.isArray(x)||canonical(Object.keys(x).sort())!==canonical([...expected].sort()))fail('記録形式の項目が一致しません');}
// JSON.parse alone accepts duplicate keys. Reject them before schema validation.
function strictParse(text){
 if(typeof text!=='string'||text.length>5000000)fail('JSONは5MB以下にしてください');let i=0;
 const ws=()=>{while(/[\x20\x09\x0a\x0d]/.test(text[i]||'!'))i++;};
 function string(){const start=i++;while(i<text.length){if(text[i]==='\\'){i+=2;continue;}if(text[i++]==='"')return JSON.parse(text.slice(start,i));}fail('JSONの文字列が壊れています');}
 function value(depth){if(depth>40)fail('JSONが深すぎます');ws();const ch=text[i];if(ch==='"')return string();if(ch==='{'){i++;const out=Object.create(null),seen=new Set();ws();if(text[i]==='}'){i++;return out;}while(true){ws();if(text[i]!=='"')fail('JSONの項目名が不正です');const key=string();if(seen.has(key)||['__proto__','prototype','constructor'].includes(key))fail('JSONの重複・禁止項目');seen.add(key);ws();if(text[i++]!==':')fail('JSONが壊れています');out[key]=value(depth+1);ws();const end=text[i++];if(end==='}')return out;if(end!==',')fail('JSONが壊れています');}}
 if(ch==='['){i++;const out=[];ws();if(text[i]===']'){i++;return out;}while(true){out.push(value(depth+1));ws();const end=text[i++];if(end===']')return out;if(end!==',')fail('JSONが壊れています');}}
 const token=/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(i));if(!token)fail('JSONが壊れています');i+=token[0].length;const parsed=JSON.parse(token[0]);if(typeof parsed==='number'&&!Number.isFinite(parsed))fail('JSONの数値が範囲外です');return parsed;}
 const result=value(0);ws();if(i!==text.length)fail('JSON末尾に余分な内容があります');return result;
}
const blank=()=>({answers:Object.fromEntries(Object.keys(CHOICES).map(k=>[k,'unconfirmed'])),memo:''});
function content(x){return {answers:x.answers,memo:x.memo};}
function validateContent(x){keys(x.answers,Object.keys(CHOICES));for(const key in CHOICES)if(!CHOICES[key].includes(x.answers[key]))fail('回答の選択肢が不正です');if(typeof x.memo!=='string'||x.memo.length>10000)fail('メモは1万文字以内にしてください');}
function timestamp(x){if(typeof x!=='string'||!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(x)||!Number.isFinite(Date.parse(x)))fail('記録日時が不正です');const normalized=x.replace(/(?:\.(\d{1,3}))?Z$/,(_,fraction)=>'.'+(fraction||'').padEnd(3,'0')+'Z');if(new Date(x).toISOString()!==normalized)fail('記録日時の暦日が不正です');return Date.parse(x);}
class ReviewStore{
 constructor(manifest){if(manifest.format_version!==VERSION||digest(manifest.targets)!==manifest.dataset_id)fail('確認画面の対象情報が壊れています');this.manifest=clone(manifest);this.targets=new Map(this.manifest.targets.map(t=>[t.target_id,t]));if(this.targets.size!==manifest.targets.length)fail('確認対象IDの重複');this.history=[];this.drafts=new Map();}
 head(id){return this.history.filter(r=>r.binding.target_id===id).at(-1)||null;}
 current(id){if(!this.targets.has(id))fail('確認対象がありません');return clone(this.drafts.get(id)||content(this.head(id)||blank()));}
 edit(id,data){if(!this.targets.has(id))fail('確認対象がありません');validateContent(data);this.drafts.set(id,clone(content(data)));}
 dirty(id){return canonical(this.current(id))!==canonical(content(this.head(id)||blank()));}
 exportJSON(at=new Date().toISOString()){
  timestamp(at);const records=clone(this.history);
  for(const [id,binding] of this.targets){const x=this.current(id),head=this.head(id);if(head&&canonical(content(head))===canonical(x))continue;
   if(head&&timestamp(at)<timestamp(head.recorded_at))fail('端末の日時が前の記録より古いため出力を止めました');
   const answered=Object.values(x.answers).some(v=>v!=='unconfirmed')||x.memo!=='';
   const r={format_version:VERSION,binding:clone(binding),...x,recorded_at:at,reviewed_at:answered?at:null,previous_record_id:head?.record_id||null};r.record_id=digest(r);records.push(r);
  }
  const doc={kind:TYPE,format_version:VERSION,dataset_id:this.manifest.dataset_id,records};doc.checksum=digest(doc);
  // Validate whole export before changing the in-memory history.
  this.validate(doc);if(canonical(doc).length>5000000)fail('履歴が5MBを超えたため出力できません');this.history=records;this.drafts.clear();return canonical(doc);
 }
 validate(doc){
  keys(doc,['kind','format_version','dataset_id','records','checksum']);if(doc.kind!==TYPE||doc.format_version!==VERSION)fail('別形式のJSONは読み込めません');if(doc.dataset_id!==this.manifest.dataset_id)fail('対象事件・判断・ハッシュ・記事版が異なる画面の記録です');
  const {checksum,...body}=doc;if(digest(body)!==checksum)fail('JSONのチェックサムが一致しません');if(!Array.isArray(doc.records)||doc.records.length>5000)fail('記録件数が不正です');
  const seen=new Set(),heads=new Map();
  for(const r of doc.records){keys(r,['format_version','binding','answers','memo','recorded_at','reviewed_at','previous_record_id','record_id']);const id=r.binding?.target_id;
   if(r.format_version!==VERSION||!this.targets.has(id)||canonical(r.binding)!==canonical(this.targets.get(id)))fail('事件・判断ID・ハッシュ・記事版が一致しません');
   validateContent(r);timestamp(r.recorded_at);const answered=Object.values(r.answers).some(v=>v!=='unconfirmed')||r.memo!=='';
   if(answered?r.reviewed_at!==r.recorded_at:r.reviewed_at!==null)fail('未確認状態または確認日時が不正です');
   const {record_id,...body}=r;if(digest(body)!==record_id)fail('記録IDが一致しません');if(seen.has(record_id))fail('重複記録があります');seen.add(record_id);
   const head=heads.get(id);if(r.previous_record_id!==(head?.record_id||null))fail('履歴の欠落・分岐・競合があります');if(head&&timestamp(r.recorded_at)<timestamp(head.recorded_at))fail('記録日時が逆転しています');heads.set(id,r);
  }return doc;
 }
 planImport(text){
  const doc=this.validate(strictParse(text)),merged=clone(this.history);let added=0,older=0;
  for(const [id] of this.targets){const local=this.history.filter(r=>r.binding.target_id===id),incoming=doc.records.filter(r=>r.binding.target_id===id);if(!incoming.length)continue;
   const shared=Math.min(local.length,incoming.length);for(let i=0;i<shared;i++)if(canonical(local[i])!==canonical(incoming[i]))fail('競合する確認結果があります。自動上書きしません');
   if(incoming.length<=local.length){if(incoming.length<local.length)older++;continue;}
   if(this.dirty(id))fail('未出力の入力と競合します。先にJSONを出力してください');
   for(const r of incoming.slice(local.length)){merged.push(clone(r));added++;}
  }
  return {base:digest([this.history,Array.from(this.drafts)]),records:merged,added,older};
 }
 applyImport(plan){if(plan.base!==digest([this.history,Array.from(this.drafts)]))fail('検証後に入力が変わりました。再検証してください');const doc={kind:TYPE,format_version:VERSION,dataset_id:this.manifest.dataset_id,records:plan.records};doc.checksum=digest(doc);this.validate(doc);for(const [id] of this.targets){const next=plan.records.filter(r=>r.binding.target_id===id).at(-1);if(next?.record_id!==this.head(id)?.record_id)this.drafts.delete(id);}this.history=clone(plan.records);return {added:plan.added,older:plan.older};}
}
const api={ReviewStore,canonical,digest,sha256,strictParse,blank,CHOICES};if(typeof module==='object'&&module.exports)module.exports=api;else root.ReviewRecords=api;
})(typeof globalThis==='object'?globalThis:this);
