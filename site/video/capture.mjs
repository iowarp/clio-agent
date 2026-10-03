import puppeteer from 'puppeteer';
import {mkdir, writeFile, readFile, readdir, unlink, access, rename} from 'node:fs/promises';
import {resolve} from 'node:path';
import {installPointerGuide} from './pointer-guide.mjs';

// Records the browser continuously. Screenshots are review evidence only.
const takeDir = resolve('public/takes');
await mkdir(takeDir, {recursive: true});
const commandDir = resolve('.capture-commands');
await mkdir(commandDir, {recursive: true});
const browser = await puppeteer.launch({headless: true, executablePath:process.env.DEMO_CHROME_PATH || undefined, userDataDir:resolve('.capture-profile'), defaultViewport: {width: 1280, height: 870, deviceScaleFactor: 1}});
const page = await browser.newPage();
let recorder;
let began;
let takeName;
let marks = [];
let closing = false;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function pointer() {
  // This editorial pointer follows the actual browser mouse events.
  await page.evaluate(installPointerGuide);
}

async function run(action) {
  if (action.op === 'viewport') {
    if(recorder) throw new Error('Set framing before starting a take');
    if(!Number.isInteger(action.width) || !Number.isInteger(action.height) || action.width<640 || action.height<480) throw new Error('Supply a viewport at least 640 x 480');
    await page.setViewport({width:action.width,height:action.height,deviceScaleFactor:action.scale??1});
  }
  if (action.op === 'goto') {await page.goto(action.url, {waitUntil:'networkidle2', timeout:60000}); await pointer();}
  if (action.op === 'inspect') return page.evaluate(() => ({title:document.title,url:location.href,text:document.body.innerText.slice(-16000), controls:[...document.querySelectorAll('button,input,textarea,a,[contenteditable],[role="button"]')].map((el) => {const r=el.getBoundingClientRect();return {tag:el.tagName,text:el.innerText,value:el.value,href:el.getAttribute('href'),aria:el.getAttribute('aria-label'),title:el.getAttribute('title'),placeholder:el.getAttribute('placeholder'),type:el.getAttribute('type'),rect:{x:r.x,y:r.y,w:r.width,h:r.height}};}).filter((el)=>el.rect.w && el.rect.h && el.rect.y>=0 && el.rect.y<innerHeight)}));
  if (action.op === 'shot') {await page.screenshot({path: resolve(takeDir, action.name + '.png')}); return resolve(takeDir, action.name + '.png');}
  if (action.op === 'click') {if(action.selector) await page.click(action.selector);else await page.mouse.click(action.x,action.y);}
  if (action.op === 'move' || action.op === 'drag') {
    const from = action.from; const to = action.to; const steps = Math.max(1, Math.ceil((action.ms ?? 1000)/25));
    await page.mouse.move(...from);
    if(action.op === 'drag') await page.mouse.down();
    for(let i=1;i<=steps;i++) {const t=i/steps; const ease=t*t*(3-2*t); await page.mouse.move(from[0]+(to[0]-from[0])*ease,from[1]+(to[1]-from[1])*ease); await pause((action.ms??1000)/steps);}
    if(action.op === 'drag') await page.mouse.up();
  }
  if (action.op === 'type') await page.keyboard.type(action.text,{delay:action.delay??65});
  if (action.op === 'fill') await page.locator(action.selector).fill(action.text);
  if (action.op === 'key') {
    const keys=action.key.split('+');
    for(const key of keys.slice(0,-1)) await page.keyboard.down(key);
    await page.keyboard.press(keys.at(-1));
    for(const key of keys.slice(0,-1).reverse()) await page.keyboard.up(key);
  }
  if (action.op === 'wheel') await page.mouse.wheel({deltaY:action.y,deltaX:0});
  if (action.op === 'wait') await pause(action.ms);
  if (action.op === 'start') {
    if(recorder) throw new Error('Stop the current take before starting another');
    if(Number((await browser.version()).match(/\/(\d+)/)?.[1])<153) throw new Error('Continuous native recording requires Chrome 153 or later');
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(action.name ?? '')) throw new Error('Use a simple, unique take name');
    takeName=action.name;
    const target=resolve(takeDir,takeName+'.mp4');
    try {await access(target); throw new Error('Take already exists; choose a new name');}
    catch(error) {if(error.code !== 'ENOENT') throw error;}
    marks=[];
    recorder=await page.record({path:target,frameRate:30,maxWidth:1280,maxHeight:870});
    began=Date.now();
  }
  if (action.op === 'mark') {if(!began) throw new Error('No take is recording');const mark={label:action.label,seconds:(Date.now()-began)/1000};marks.push(mark);return mark;}
  if (action.op === 'stop') {if(!recorder) throw new Error('No take is recording'); await recorder.stop(); recorder=undefined; began=undefined; await writeFile(resolve(takeDir,takeName+'.json'),JSON.stringify({source:takeName+'.mp4',fps:30,marks},null,2));return marks;}
  if (action.op === 'close') {if(recorder) await run({op:'stop'}); await browser.close(); closing=true;}
  return {ok:true};
}

console.log('CAPTURE_READY');
while (!closing) {
  const files = (await readdir(commandDir)).filter((name) => name.endsWith('.command.json')).sort();
  for (const name of files) {
    let result;
    try {result=await run(JSON.parse((await readFile(resolve(commandDir,name),'utf8')).replace(/^\uFEFF/,'')));}
    catch(error) {result={error:String(error)};}
    const replyPath=resolve(commandDir,name.replace('.command.json','.reply.json'));
    await writeFile(replyPath+'.pending',JSON.stringify(result,null,2));
    await rename(replyPath+'.pending',replyPath);
    await unlink(resolve(commandDir,name));
    console.log(name,JSON.stringify(result).slice(0,1000));
  }
  await pause(100);
}
