import {sendCommand as command} from './capture-client.mjs';
import puppeteer from 'puppeteer';
import {readFile} from 'node:fs/promises';

const name = process.argv[2];
if (!name) throw new Error('Supply a unique take name');
const mark = label => command({op:'mark', label});
const wait = ms => command({op:'wait', ms});
let cursor = [1145, 355];
async function click(control) {
  if (!control) throw new Error('Required control missing; retain recording');
  const next = [control.rect.x+control.rect.w/2, control.rect.y+control.rect.h/2];
  await command({op:'move', from:cursor, to:next, ms:260});
  await command({op:'click', x:next[0], y:next[1]});
  cursor = next;
}
await command({op:'start', name});
await mark('previous-figure');
await wait(300);
let state = await command({op:'inspect'});
await click(state.controls.find(c=>c.aria==='Exit full screen'));
state = await command({op:'inspect'});
await click(state.controls.find(c=>c.aria?.startsWith('Ask CLIO')));
await mark('typing');
await command({op:'type', text:'Make a new detailed close-up of the stall using Natural Earth 10m coastlines, 79–76°W and 25–28°N. Label both 1 September peak UTC times.', delay:0});
await mark('typed');
state = await command({op:'inspect'});
await click(state.controls.find(c=>c.aria==='Submit'));
await mark('submitted');
await wait(1600);
state = await command({op:'inspect'});
const bottom=state.controls.find(c=>c.aria==='Scroll to bottom');
if(bottom) await click(bottom);
await mark('working');
let observedWork=false;
for(let i=0;i<300;i++) {
  state=await command({op:'inspect'});
  const idle=state.text.includes('Session details\nNo active work');
  observedWork ||= !idle;
  if(observedWork && idle) {
    await mark('answer-arrived');
    await command({op:'shot', name:name+'-inline-answer'});
    await wait(3800);
    const [port,path]=(await readFile('.capture-profile/DevToolsActivePort','utf8')).trim().split(/\r?\n/);
    const browser=await puppeteer.connect({browserWSEndpoint:`ws://127.0.0.1:${port}${path}`,defaultViewport:null});
    const page=(await browser.pages()).find(p=>p.url().includes(':5176/'));
    const fullscreen=await page.$$eval('[data-slot="a2ui-media-image"] button[aria-label="Full screen"]',buttons=>buttons.map(button=>{
      const r=button.getBoundingClientRect();
      return {rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    }).filter(b=>b.rect.w && b.rect.y>=0 && b.rect.y<innerHeight).at(-1));
    await browser.disconnect();
    await click(fullscreen);
    await mark('image-expanded');
    await wait(4800);
    await command({op:'shot',name:name+'-full-image'});
    console.log(JSON.stringify(await command({op:'stop'})));
    process.exit(0);
  }
  await wait(1600);
}
throw new Error('Response timed out; retain recording');
