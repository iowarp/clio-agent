import puppeteer from 'puppeteer';
import {readFile, writeFile} from 'node:fs/promises';
const [port,path]=(await readFile('.capture-profile/DevToolsActivePort','utf8')).trim().split(/\r?\n/);
const browser=await puppeteer.connect({browserWSEndpoint:`ws://127.0.0.1:${port}${path}`,defaultViewport:null});
const page=(await browser.pages()).find(p=>p.url().includes(':5176/'));
if(!page) throw new Error('Recorder demo page is missing');
if(process.argv[2]==='save') {
  const data=await page.evaluate(()=>({audit:window.__followAudit,viewport:[innerWidth,innerHeight],log:[...document.querySelectorAll('[role="log"]')].map(e=>({top:e.scrollTop,height:e.scrollHeight,client:e.clientHeight,rect:e.getBoundingClientRect().toJSON()}))}));
  await writeFile(process.argv[3]??'out/follow-audit.json',JSON.stringify(data,null,2));
  console.log(JSON.stringify(data.log));
} else {
  await page.evaluate(()=>{
    window.__followAudit=[];
    const add=(kind,el,extra={})=>{
      if(el?.getAttribute('role')!=='log')return;
      window.__followAudit.push({time:performance.now(),kind,top:el.scrollTop,height:el.scrollHeight,client:el.clientHeight,...extra});
      if(window.__followAudit.length>5000)window.__followAudit.shift();
    };
    const original=Element.prototype.scrollTo;
    Element.prototype.scrollTo=function(...args){add('scrollTo-before',this,{args,stack:new Error().stack});const result=original.apply(this,args);add('scrollTo-after',this);return result;};
    const into=Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView=function(...args){const log=this.closest('[role="log"]');add('scrollIntoView',log,{target:this.id,args,stack:new Error().stack});return into.apply(this,args);};
    const descriptor=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');
    if(descriptor?.set)Object.defineProperty(Element.prototype,'scrollTop',{...descriptor,set(value){add('setTop',this,{value,stack:new Error().stack});descriptor.set.call(this,value);}});
    const log=document.querySelector('[role="log"]');
    if(!log)throw new Error('Transcript log is missing');
    for(const event of ['scroll','scrollend','wheel','pointerdown','keydown'])log.addEventListener(event,e=>add(event,log,{deltaY:e.deltaY,key:e.key,target:e.target.tagName}),{capture:true});
    new ResizeObserver(()=>add('resize',log)).observe(log.firstElementChild);
    add('start',log);
  });
  console.log('Follow audit installed; viewport preserved');
}
await browser.disconnect();
