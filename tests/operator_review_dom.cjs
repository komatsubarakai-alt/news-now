// Run real page JavaScript against a small DOM double; this does not test layout.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const data=JSON.parse(html.match(/id="snapshot">([\s\S]*?)<\/script>/)[1]);
const code=html.match(/<script>\n([\s\S]*?)<\/script>/)[1];
class Node{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this.hidden=false;this.disabled=false;this.attrs={};this._text='';this._value='';}
 set textContent(s){this._text=String(s);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join(' ');}
 set value(v){this._value=v;}get value(){return this._value;}
 append(...ns){this.children.push(...ns);}replaceChildren(...ns){this.children=ns;this._text='';}
 addEventListener(type,fn){this.listeners[type]=fn;}removeAttribute(a){delete this.attrs[a];delete this[a];}
 click(){if(this.download)downloads.push(this);return this.listeners.click?.();}scrollIntoView(){}
}
let downloads=[],blobs=[];const store=new Map();
const nodes={};for(const id of ['snapshot','stamp','search','category','filter','stats','export','restore','clear','file','message','cards','page','prev','next'])nodes[id]=new Node();
nodes.snapshot.textContent=JSON.stringify(data);nodes.filter.value='all';
class TestURL extends URL{static createObjectURL(blob){blobs.push(blob);return 'blob:test';}static revokeObjectURL(){}}
const context=vm.createContext({document:{getElementById:id=>nodes[id],createElement:tag=>new Node(tag)},
 localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},Date,Intl,URL:TestURL,
 Blob:class{constructor(parts){this.parts=parts;}},setTimeout:()=>{},confirm:()=>true,console});
const run=s=>vm.runInContext(s,context);
(async()=>{
 run(code);assert.equal(nodes.cards.children.length,6);assert(nodes.stats.textContent.includes(`確認待ち ${data.reviews.length}件`));assert(nodes.export.disabled);
 nodes.next.click();assert(nodes.page.textContent.startsWith('2 /'));nodes.prev.click();
 const row=data.reviews[0];run(`drafts[${JSON.stringify(row.review_key)}]={action:'same_event',event_id:${JSON.stringify(data.events[0].event_id)},note:''};render()`);
 nodes.export.click();assert(nodes.message.textContent.includes('判断理由'));assert.equal(downloads.length,0);
 run(`drafts[${JSON.stringify(row.review_key)}].note='記事の事件名と関係者を確認';saved();render()`);
 nodes.export.click();assert.equal(downloads.length,1);
 const exported=JSON.parse(blobs[0].parts[0]);assert.equal(exported.decisions.length,1);assert.equal(exported.decisions[0].group_hash,row.group_hash);
 assert.equal(exported.decisions[0].target_hash,data.events[0].target_hash);
 nodes.filter.value='candidate';nodes.filter.listeners.change();assert(nodes.page.textContent.includes(`${data.reviews.filter(r=>r.exact_heading_candidate_event_ids.length).length}件`));
 nodes.filter.value='decided';nodes.filter.listeners.change();assert.equal(nodes.cards.children.length,1);
 nodes.search.value='no-matching-article-xyz';nodes.search.listeners.input();assert(nodes.cards.textContent.includes('条件に合う記事はありません'));
 const stale=JSON.parse(JSON.stringify(exported));stale.decisions[0].group_hash='stale';
 nodes.file.files=[{text:async()=>JSON.stringify(stale)}];await nodes.file.listeners.change();assert(nodes.message.textContent.includes('古い'));
 nodes.file.files=[{text:async()=>JSON.stringify(exported)}];await nodes.file.listeners.change();assert(nodes.message.textContent.includes('下書きに読み込みました'));
 nodes.clear.click();assert(nodes.stats.textContent.includes('判断済み 0件'));
 assert(!run(`link({title:'bad',url:'javascript:alert(1)'}).href`));
 assert.equal(run(`node('p','<img onerror=alert(1)>').children.length`),0);
 console.log('Review DOM passed: render/count, pagination, routing, required reason, export, candidates, filters, stale import, restore, draft reset, safe text/URL. Layout not tested.');
})().catch(e=>{console.error(e);process.exitCode=1});
