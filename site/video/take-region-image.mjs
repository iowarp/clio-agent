import {sendCommand as command} from './capture-client.mjs';
import puppeteer from 'puppeteer';
import {readFile} from 'node:fs/promises';

// r12 framing: collapsed sidebar, Dorian map zoomed with real wheel gestures.
const name=process.argv[2];
if(!name) throw new Error('Supply a new, unique recording name');
const mark=label=>command({op:'mark',label});
const wait=ms=>command({op:'wait',ms});
const center=c=>[c.rect.x+c.rect.w/2,c.rect.y+c.rect.h/2];
let cursor;
async function inspect(predicate) {
  const state=await command({op:'inspect'});
  const control=state.controls.filter(predicate).at(-1);
  if(!control) throw new Error('Required control missing; recording remains active');
  return control;
}
async function move(target,ms=260) {
  const to=Array.isArray(target)?target:center(target);
  await command({op:'move',from:cursor??to,to,ms});
  cursor=to;
}
async function click(control) {
  await move(control);
  await command({op:'click',x:cursor[0],y:cursor[1]});
}
const opening=await command({op:'inspect'});
if(!opening.text.includes('Bypass checks') || !opening.text.includes('No active work')) {
  throw new Error('Prepare an idle bypass demo session before recording');
}
const capture=await inspect(c=>c.aria==='Capture labelled regions');
await move(capture,25);
await command({op:'start',name});
await mark('opening');
await wait(600);
await click(capture);
await move([735,435],300);
await mark('region-drag');
await command({op:'drag',from:cursor,to:[875,500],ms:1400});
cursor=[875,500];
await mark('region-selected');
await click(await inspect(c=>c.aria?.startsWith('Comment for S')));
await command({op:'type',text:'Show where Dorian stalls.',delay:2});
await click(await inspect(c=>c.text==='Done'));
await mark('comment-added');
await click(await inspect(c=>c.text==='Add 1 region to message'));
await mark('image-attached');
await wait(450);
await click(await inspect(c=>c.aria?.startsWith('Open clio-capture-')));
await mark('attached-image-opened');
await wait(2000);
await command({op:'key',key:'Escape'});
await click(await inspect(c=>c.aria==='Close capture'));
await click(await inspect(c=>c.aria?.startsWith('Ask CLIO')));
await mark('typing');
const prompt='Create a close-up PNG of S1, with coastlines and both peak UTC labels.';
await command({op:'type',text:prompt,delay:0});
await mark('typed');
await click(await inspect(c=>c.aria==='Submit'));
await mark('submitted');
await wait(1600);
let state=await command({op:'inspect'});
const follow=state.controls.find(c=>c.aria==='Scroll to bottom');
if(follow) await click(follow);
await mark('working');
for(let i=0;i<300;i++) {
  state=await command({op:'inspect'});
  if(state.text.includes('Response unavailable') || state.text.includes('response needed')) {
    throw new Error('Live turn needs attention; recording remains active');
  }
  if(state.text.includes('Session details\nNo active work') && state.text.includes(prompt)) {
    await mark('answer-arrived');
    await command({op:'shot',name:name+'-inline-answer'});
    await wait(3800);
    // Only the generated Image owns this toolbar; a map's button is unrelated.
    const [port,path]=(await readFile('.capture-profile/DevToolsActivePort','utf8')).trim().split(/\r?\n/);
    const browser=await puppeteer.connect({browserWSEndpoint:`ws://127.0.0.1:${port}${path}`,defaultViewport:null});
    const page=(await browser.pages()).find(p=>p.url().includes(':5176/'));
    const fullscreen=await page.$$eval('[data-slot="a2ui-media-image"] button[aria-label="Full screen"]',buttons=>buttons.map(button=>{
      const r=button.getBoundingClientRect();
      return {rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    }).filter(b=>b.rect.w && b.rect.y>=0 && b.rect.y<innerHeight).at(-1));
    await browser.disconnect();
    if(!fullscreen) throw new Error('Agent did not present its image with A2UI; retain rejected take');
    await click(fullscreen);
    await mark('image-expanded');
    await wait(4800);
    await command({op:'shot',name:name+'-full-image'});
    console.log(JSON.stringify(await command({op:'stop'})));
    process.exit(0);
  }
  if(i%5===0) await command({op:'shot',name:name+'-activity-'+i});
  await wait(1600);
}
throw new Error('Live response timed out; recording remains active');
