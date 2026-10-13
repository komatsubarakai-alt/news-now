// Offline DOM double: tests actual production JavaScript, not visual layout.
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert');
class Node {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.attrs={};this.listeners={};this._text='';}
 set textContent(s){this._text=String(s);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join(' ');}
 append(...nodes){this.children.push(...nodes);}
 appendChild(n){this.children.push(n);return n;}
 replaceChildren(...nodes){this.children=nodes;this._text='';}
 setAttribute(k,v){this.attrs[k]=String(v);}
 addEventListener(k,f){this.listeners[k]=f;}
 insertAdjacentElement(p,n){this.adjacent=n;}
 querySelectorAll(){return [];}
}
(async()=>{
 const output=path.resolve(process.argv[2]);const summary=[];
 for(const version of ['baseline','proposed'])for(const phase of ['before','after']) {
  const folder=path.join(output,version,phase),html=fs.readFileSync(path.join(folder,'index.html'),'utf8');
  const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const data=JSON.parse(fs.readFileSync(path.join(folder,'public_events.json'),'utf8'));
  const nodes={status:new Node(),news:new Node()};
  const errors=[];
  const context=vm.createContext({document:{getElementById:k=>nodes[k],createElement:t=>new Node(t)},
   Date,Intl,URL,console:{error:e=>errors.push(String(e))},fetch:async url=>({ok:true,json:async()=>url.startsWith('public_events')?data:{}})});
  vm.runInContext(code.replace('loadNews();',''),context);await vm.runInContext('loadNews()',context);
  assert.deepEqual(errors,[]);
  for(const [id,count,stage] of [
   ['b565866f-bfb4-4888-b054-97e69d71a3f2',3,'判決'],
   ['5d4bafaf-011f-4e26-9014-22a57b3fef2b',1,'正式決定'],
   ['e98c5d08-4d42-4ae4-8b60-294778a05c75',2,'調査結果公表']]) {
   const card=nodes.news.children.find(c=>c.id==='event-'+id);assert(card,'event rendered');
   const event=data.find(e=>e.event_id===id),text=card.textContent;
   assert(text.includes('進展'+(phase==='after'?count:0)+'回'));
   for(const value of [event.current_stage,event.latest,event.next_watch])assert(text.includes(value));
   for(const a of event.articles)assert(text.includes(a.title));
   if(phase==='after') {
    assert(text.includes('出来事の進展を見る（'+count+'回）'));
    assert(text.includes(id.startsWith('b565')?'2026/11/03 10:10':id.startsWith('5d4')?'2026/11/01 10:10':'2026/11/02 10:10'));
    if(version==='proposed')for(const p of event.progress_history){assert(text.includes('根拠記事公開：'));for(const proof of p.progress_evidence)assert(text.includes('引用：'+proof.quote));}
   }
  }
  summary.push({version,phase,result:'pass',coverage:'3 event cards: count/current/latest/next/timeline/JST timestamp/evidence text',limitation:'DOM double; layout/mobile/browser screenshot not verified'});
 }
 fs.writeFileSync(path.join(output,'dom-results.json'),JSON.stringify(summary,null,2));console.log(JSON.stringify(summary,null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
