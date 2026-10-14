// Real page JavaScript on a DOM double. Rendering/layout is not covered.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const data=JSON.parse(html.match(/id="snapshot" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const script=html.match(/<script>\n([\s\S]*?)<\/script>/)[1];
class Node{
 constructor(tag){this.tag=tag;this.children=[];this.listeners={};this._text='';}
 set textContent(v){this._text=String(v);this.children=[]}get textContent(){return this._text+this.children.map(x=>x.textContent).join(' ')}
 append(...nodes){this.children.push(...nodes)}replaceChildren(...nodes){this.children=nodes;this._text=''}
 addEventListener(type,fn){this.listeners[type]=fn}click(){if(this.download)downloads.push(this);return this.listeners.click?.()}
}
const nodes={};for(const id of ['snapshot','stats','cards','message','export','import','index'])nodes[id]=new Node('div');nodes.snapshot.textContent=JSON.stringify(data);
const stored=new Map(),downloads=[],blobs=[];
class TestURL extends URL{static createObjectURL(b){blobs.push(b);return 'blob:test'}static revokeObjectURL(){}}
const context=vm.createContext({document:{getElementById:id=>nodes[id],createElement:tag=>new Node(tag)},localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)},URL:TestURL,Date,Blob:class{constructor(parts){this.parts=parts}},setTimeout:()=>{},console});
const run=code=>vm.runInContext(code,context);
function all(root,predicate){return [root,...root.children.flatMap(n=>all(n,predicate))].filter(predicate)}
(async()=>{
 run(script);assert.equal(nodes.cards.children.length,15);assert(nodes.stats.textContent.includes('0 / 15'));
 const card=nodes.cards.children[0],note=all(card,n=>n.tag==='textarea')[0];note.value='本文未確認';note.listeners.input();assert(nodes.stats.textContent.includes('0 / 15'));
 nodes.export.click();assert.equal(downloads.length,0);
 const radio=all(card,n=>n.type==='radio')[0];radio.listeners.change();assert(nodes.stats.textContent.includes('1 / 15'));
 nodes.export.click();assert.equal(downloads.length,1);const exported=JSON.parse(blobs[0].parts[0]);assert.equal(exported.records[0].note,'本文未確認');assert.equal(exported.records[0].choice,'same_event');assert.equal(exported.records[0].record_scope,'review_only');assert.equal(exported.records[0].incoming_body_checked,false);
 for(const mutate of [v=>v.dataset_sha256='stale',v=>v.records[0].event_id='other',v=>v.records[0].evidence_sha256='stale',v=>v.records.push(v.records[0]),v=>v.records[0].sample_id='__proto__',v=>v.records[0].choice='new_event',v=>v.records[0].record_scope='gold',v=>v.records[0].incoming_body_checked='true']){
  const value=JSON.parse(JSON.stringify(exported));mutate(value);nodes.import.files=[{text:async()=>JSON.stringify(value),size:1000}];await nodes.import.listeners.change({target:nodes.import});assert(nodes.message.textContent.includes('読み込みできません'));assert(nodes.stats.textContent.includes('1 / 15'));
 }
 run('drafts=Object.create(null);render()');nodes.import.files=[{text:async()=>JSON.stringify(exported),size:1000}];await nodes.import.listeners.change({target:nodes.import});assert(nodes.message.textContent.includes('読み込みました'));assert(nodes.stats.textContent.includes('1 / 15'));
 assert(!run("link({title:'bad',url:'javascript:alert(1)'}).href"));assert.equal(run("node('p','<script>alert(1)</script>').children.length"),0);
 console.log('Review DOM: 15 pairs, no implicit decision, JSON round trip, 8 invalid imports rejected atomically, safe text/URL passed. No browser layout test.');
})().catch(e=>{console.error(e);process.exitCode=1});
