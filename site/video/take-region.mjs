import {sendCommand as command} from './capture-client.mjs';

// Coordinates belong to the inspected collapsed-sidebar Dorian map. Reinspect
// before reusing this script in another session, map zoom or viewport.
const takeName=process.argv[2];
if(!takeName) throw new Error('Usage: node take-region.mjs NEW_UNIQUE_TAKE_NAME');
const mark=label=>command({op:'mark',label});
const wait=ms=>command({op:'wait',ms});
const center=control=>[control.rect.x+control.rect.w/2,control.rect.y+control.rect.h/2];
let cursor;
async function moveTo(control,ms=300) {
  const next=Array.isArray(control)?control:center(control);
  await command({op:'move',from:cursor??next,to:next,ms});
  cursor=next;
}
async function inspectControl(predicate) {
  const state=await command({op:'inspect'});
  const found=state.controls.find(predicate);
  if(!found) throw new Error('Required visible control is missing; recording remains active');
  return found;
}
async function clickControl(control) {
  await moveTo(control);
  await command({op:'click',x:cursor[0],y:cursor[1]});
}
const opening=await command({op:'inspect'});
if(!opening.text.includes('Bypass checks')) throw new Error('Prepare the user-requested demo bypass policy before recording');
const camera=opening.controls.find(item=>item.aria==='Capture labelled regions');
if(!camera) throw new Error('Map capture control is missing');
await moveTo(camera,25);
await command({op:'start',name:takeName});
await mark('opening');
await wait(700);
await clickControl(camera);
await moveTo([615,440],350);
await mark('region-drag');
await command({op:'drag',from:cursor,to:[850,550],ms:1800});
cursor=[850,550];
await mark('region-selected');
await clickControl(await inspectControl(item=>item.aria?.startsWith('Comment for S')));
await command({op:'type',text:'Show where Dorian stalls.',delay:8});
await clickControl(await inspectControl(item=>item.text==='Done'));
await mark('comment-added');
await clickControl(await inspectControl(item=>item.text==='Add 1 region to message'));
await mark('image-attached');
await wait(700);
await clickControl(await inspectControl(item=>item.aria?.startsWith('Open clio-capture-')));
await mark('attached-image-opened');
await wait(2200);
await command({op:'key',key:'Escape'});
await clickControl(await inspectControl(item=>item.aria==='Close capture'));
await clickControl(await inspectControl(item=>item.aria?.startsWith('Ask CLIO to investigate')));
await mark('typing');
const prompt='Attach a PNG map of this bend with coastlines and peak UTC labels.';
await command({op:'type',text:prompt,delay:8});
await mark('typed');
await clickControl(await inspectControl(item=>item.aria==='Submit'));
await mark('submitted');
await wait(1800);
const working=await command({op:'inspect'});
if(working.controls.some(item=>item.aria==='Scroll to bottom')) {
  await clickControl(working.controls.find(item=>item.aria==='Scroll to bottom'));
}
await mark('working');
for(let attempt=0;attempt<300;attempt++) {
  const state=await command({op:'inspect'});
  if(state.text.includes('response needed') || state.text.includes('Response unavailable')) {
    throw new Error('Real run needs attention; recording remains active');
  }
  if(state.text.includes('Session details\nNo active work') && state.text.includes(prompt)) {
    await mark('real-answer-arrived');
    await moveTo([1100,800]);
    await command({op:'wheel',y:750});
    await wait(500);
    const file=await inspectControl(item=>item.aria?.startsWith('Open ') && item.aria.endsWith('.png') && !item.aria.includes('clio-capture-'));
    await command({op:'shot',name:takeName+'-prompt-and-answer'});
    await wait(2500);
    await clickControl(file);
    await mark('generated-image-opened');
    const maximize=await inspectControl(item=>item.aria==='Maximize canvas');
    await clickControl(maximize);
    await mark('image-expanded');
    await wait(5500);
    await command({op:'shot',name:takeName+'-expanded-image'});
    console.log(JSON.stringify(await command({op:'stop'}),null,2));
    process.exit(0);
  }
  await wait(2000);
}
throw new Error('Response timed out; recording remains active');
