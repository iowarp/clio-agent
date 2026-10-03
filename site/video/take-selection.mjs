import {writeFile,rename,readFile,unlink} from 'node:fs/promises';
import {resolve} from 'node:path';

// Run only after inspecting the live map at the configured fixed viewport.
// A fresh dated filename is required; archived takes must never be replaced.
const takeName = process.argv[2];
if (!takeName) throw new Error('Usage: node take-selection.mjs NEW_UNIQUE_TAKE_NAME');
let serial=0;
async function command(action) {
  const stem=resolve('.capture-commands',`${Date.now()}_${String(serial++).padStart(4,'0')}`);
  await writeFile(stem+'.pending',JSON.stringify(action));
  await rename(stem+'.pending',stem+'.command.json');
  for(let i=0;i<600;i++) {
    try {
      const result=JSON.parse(await readFile(stem+'.reply.json','utf8'));
      await unlink(stem+'.reply.json');
      if(result.error) throw new Error(result.error);
      return result;
    } catch(error) {if(error.code!=='ENOENT') throw error;}
    await new Promise((resolve)=>setTimeout(resolve,100));
  }
  throw new Error('Capture command timed out');
}
const mark=(label)=>command({op:'mark',label});
const move=(from,to,ms)=>command({op:'move',from,to,ms});
const wait=(ms)=>command({op:'wait',ms});
const center=(control)=>[control.rect.x+control.rect.w/2,control.rect.y+control.rect.h/2];
const control=async(aria)=>{
  const state=await command({op:'inspect'});
  const found=state.controls.find((item)=>item.aria===aria);
  if(!found) throw new Error(`Visible control missing: ${aria}`);
  return center(found);
};
const box=await control('Box select map points');
const dragStart=[753,434],dragEnd=[942,508];

await command({op:'move',from:box,to:box,ms:25});
await command({op:'start',name:takeName});
await mark('opening');
await wait(900);
await mark('select');
await command({op:'click',selector:'button[aria-label="Box select map points"]'});
await move(box,dragStart,350);
await mark('drag');
await command({op:'drag',from:dragStart,to:dragEnd,ms:1800});
await wait(500);
await mark('selected');
const reference=await control('Reference this');
await move(dragEnd,reference,350);
await wait(200);
await command({op:'click',selector:'button[aria-label="Reference this"]'});
await wait(500);
await mark('attached');
const composer=await control('Ask CLIO to investigate, build, explain, or act…');
await move(reference,composer,350);
await command({op:'click',selector:'[contenteditable=true]'});
await mark('typing');
await command({op:'type',text:'When were the winds strongest here?',delay:8});
await wait(300);
await mark('typed');
const submit=await control('Submit');
await move(composer,submit,350);
await command({op:'click',selector:'button[aria-label="Submit"]'});
await mark('submitted');
await wait(1800);
const workingState=await command({op:'inspect'});
if(workingState.controls.some((item)=>item.aria==='Scroll to bottom')) await command({op:'click',selector:'button[aria-label="Scroll to bottom"]'});
await mark('working');
await command({op:'shot',name:takeName+'-working'});
for(let attempt=0;attempt<120;attempt++) {
  const state=await command({op:'inspect'});
  if(state.text.includes('response needed')) throw new Error('Agent needs attention; recording remains active');
  if(state.text.includes('Response unavailable') || state.text.includes('turn finalize raised:')) throw new Error('Real response failed; retain this take as rejected, recording remains active');
  const idle=state.text.includes('Session details\nNo active work') && !state.controls.some((item)=>item.aria==='Stop');
  if(idle && state.text.includes('When were the winds strongest')) {
    await mark('answer');
    if(state.controls.some((item)=>item.aria==='Scroll to bottom')) await command({op:'click',selector:'button[aria-label="Scroll to bottom"]'});
    await command({op:'shot',name:takeName+'-answer'});
    await move(submit,[1200,650],300);
    await wait(5000);
    const marks=await command({op:'stop'});
    console.log(JSON.stringify(marks,null,2));
    process.exit(0);
  }
  await wait(2000);
}
throw new Error('Response timed out; recording remains active');
