import {sendCommand as command} from './capture-client.mjs';

const takeName = process.argv[2];
if (!takeName) throw new Error('Supply a unique take name');
const mark = label => command({op: 'mark', label});
const wait = ms => command({op: 'wait', ms});
let cursor;
async function click(control) {
  const next = [control.rect.x + control.rect.w / 2, control.rect.y + control.rect.h / 2];
  await command({op: 'move', from: cursor ?? next, to: next, ms: 280});
  cursor = next;
  await command({op: 'click', x: next[0], y: next[1]});
}
const opening = await command({op: 'inspect'});
if (!opening.text.includes('Bypass checks')) throw new Error('Demo bypass policy is required');
const composer = opening.controls.find(item => item.aria?.startsWith('Ask CLIO'));
if (!composer) throw new Error('Composer missing');
await click(composer);
await command({op: 'start', name: takeName});
await mark('review-request');
await command({op: 'type', text: 'Label both 160 kt observations with their correct UTC times.', delay: 2});
await mark('typed');
let state = await command({op: 'inspect'});
await click(state.controls.find(item => item.aria === 'Submit'));
await mark('submitted');
await wait(1800);
state = await command({op: 'inspect'});
const bottom = state.controls.find(item => item.aria === 'Scroll to bottom');
if (bottom) await click(bottom);
await mark('working');
let observedWork = false;
for (let attempt = 0; attempt < 300; attempt++) {
  state = await command({op: 'inspect'});
  if (state.text.includes('response needed') || state.text.includes('Response unavailable')) {
    throw new Error('Real run needs attention; recording remains active');
  }
  const idle = state.text.includes('Session details\nNo active work');
  observedWork ||= !idle;
  if (observedWork && idle) {
    await mark('checked-answer-arrived');
    await command({op: 'move', from: cursor, to: [1100, 800], ms: 250});
    cursor = [1100, 800];
    await command({op: 'wheel', y: 850});
    await wait(600);
    state = await command({op: 'inspect'});
    const file = state.controls.filter(item => item.aria?.startsWith('Open ') && item.aria.endsWith('.png') && !item.aria.includes('clio-capture-')).at(-1);
    if (!file) throw new Error('No generated PNG; recording remains active');
    await command({op: 'shot', name: takeName + '-answer'});
    await wait(2200);
    await click(file);
    await mark('checked-image-opened');
    state = await command({op: 'inspect'});
    await click(state.controls.find(item => item.aria === 'Maximize canvas'));
    await mark('checked-image-expanded');
    await wait(5500);
    await command({op: 'shot', name: takeName + '-image'});
    console.log(JSON.stringify(await command({op: 'stop'}), null, 2));
    process.exit(0);
  }
  await wait(2000);
}
throw new Error('Response timed out; recording remains active');
