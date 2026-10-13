// Local UI checks only. Run with Playwright installed, no external network needed.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('assert');
const root=path.resolve(process.argv[2]);
const html=fs.readFileSync(path.join(root,'news-now-manual-review.html'));
const snapshot=JSON.parse(html.toString().match(/id="snapshot">([\s\S]*?)<\/script>/)[1]);
const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true});
 try{
  const results=[];
  for(const [device,viewport]of[['mobile',{width:390,height:844}],['desktop',{width:1440,height:1000}]]){
   const context=await browser.newContext({viewport,acceptDownloads:true});
   const page=await context.newPage(),errors=[],external=[];
   page.on('pageerror',e=>errors.push(e.message));
   page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1')&&!r.url().startsWith('blob:'))external.push(r.url());});
   await page.goto(`http://127.0.0.1:${server.address().port}/`);
   assert((await page.locator('#stats').innerText()).includes('確認待ち 63件'));
   assert.equal(await page.locator('article.card').count(),6);
   assert.equal(await page.locator('#export').isDisabled(),true);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.getByRole('button',{name:'次へ',exact:true}).click();
   assert((await page.locator('#page').innerText()).startsWith('2 /'));
   await page.getByRole('button',{name:'前へ',exact:true}).click();
   const first=page.locator('article.card').first();
   await first.getByLabel('振り分け',{exact:true}).selectOption('same_event');
   const row=snapshot.reviews[0],id=row.exact_heading_candidate_event_ids[0]||snapshot.events[0].event_id;
   await first.locator('select[id^=target-]').selectOption(id);
   assert((await first.locator('.target').innerText()).includes(snapshot.events.find(e=>e.event_id===id).event_name));
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.locator('#export').click();
   assert((await page.locator('#message').innerText()).includes('判断理由'));
   await first.getByLabel('判断理由',{exact:true}).fill('記事本文の事件名と関係者を確認');
   const downloadWait=page.waitForEvent('download');await page.locator('#export').click();
   const download=await downloadWait,out=path.join(root,`ui-export-${device}.json`);await download.saveAs(out);
   const exported=JSON.parse(fs.readFileSync(out));assert.equal(exported.decisions.length,1);
   assert.equal(exported.decisions[0].action,'same_event');assert.equal(exported.decisions[0].event_id,id);
   assert.equal(exported.decisions[0].group_hash,row.group_hash);
   await page.reload();
   assert.equal(await page.locator('article.card').first().getByLabel('判断理由',{exact:true}).inputValue(),'記事本文の事件名と関係者を確認');
   await page.locator('#filter').selectOption('candidate');
   assert((await page.locator('#page').innerText()).includes('9件'));
   await page.locator('#filter').selectOption('decided');
   assert.equal(await page.locator('article.card').count(),1);
   const stale=structuredClone(exported);stale.decisions[0].group_hash='stale';
   await page.locator('#file').setInputFiles({name:'stale.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(stale))});
   assert((await page.locator('#message').innerText()).includes('古い'));
   await page.locator('#filter').selectOption('all');
   await page.screenshot({path:path.join(root,`operator-review-${device}.png`)});
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
   results.push({device,viewport,passed:['63 records / 6 per page','9 heading candidates','routing selection','required reason','JSON export','draft reload','filters/pagination','stale import rejected','no horizontal overflow','no JS errors','no external data requests']});
   await context.close();
  }
  // HTML-in-JSON and unsafe URL never turn into executable markup/links.
  const unsafe=structuredClone(snapshot);unsafe.reviews[0].event_name='<img src=x onerror="window.injected=1">';
  unsafe.reviews[0].articles[0].url='javascript:window.injected=1';
  const escaped=JSON.stringify(unsafe).replace(/</g,'\\u003c');
  const malicious=html.toString().replace(/(id="snapshot">)[\s\S]*?(<\/script>)/,`$1${escaped}$2`);
  const p=await browser.newPage();await p.setContent(malicious);
  assert.equal(await p.evaluate(()=>window.injected),undefined);
  assert.equal(await p.locator('article.card').first().locator('a[href]').count(),0);
  await p.close();
  fs.writeFileSync(path.join(root,'ui-results.json'),JSON.stringify(results,null,2));
  console.log('Mobile/desktop review UI and injection safety passed');
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
