import {sendCommand as command} from './capture-client.mjs';

// Review-only preparation. Never invoke while a take is recording.
let state = await command({op: 'inspect'});
const center = control => [control.rect.x + control.rect.w / 2, control.rect.y + control.rect.h / 2];
async function click(control) {
  const [x, y] = center(control);
  await command({op: 'click', x, y});
}
const restore = state.controls.find(item => item.aria === 'Restore canvas beside conversation');
if (restore) await click(restore);
state = await command({op: 'inspect'});
const closeCanvas = state.controls.find(item => item.aria === 'Close workspace canvas');
if (closeCanvas) await click(closeCanvas);
state = await command({op: 'inspect'});
if (state.text.includes('Search\nCtrl K')) {
  const sidebar = state.controls.find(item => item.text === 'Toggle Sidebar');
  if (sidebar) await click(sidebar);
}
state = await command({op: 'inspect'});
console.log(JSON.stringify({text: state.text, controls: state.controls.filter(item => /Zoom|Capture|Jump to/.test(item.aria ?? ''))}, null, 2));
