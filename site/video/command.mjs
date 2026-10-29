import {writeFile, rename, readFile, unlink} from 'node:fs/promises';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
const command = JSON.parse(process.argv[2] ?? '{}');
if (!command.op) throw new Error('Supply a JSON command with an op');
const id = Date.now().toString() + '-' + randomUUID();
const base = resolve(process.env.CAPTURE_COMMANDS || '.capture-commands', id);
await writeFile(base+'.pending', JSON.stringify(command));
await rename(base+'.pending', base+'.command.json');
// CAPTURE_WAIT_S: how long to wait for the recorder's reply (a slow page load under load).
const attempts = Math.round(Number(process.env.CAPTURE_WAIT_S || 60) * 4);
for (let attempt=0; attempt<attempts; attempt++) {
  try {const reply=await readFile(base+'.reply.json','utf8'); console.log(reply);await unlink(base+'.reply.json');process.exit(JSON.parse(reply).error ? 1 : 0);}
  catch(error) {if(error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;}
  await new Promise((resolve)=>setTimeout(resolve,250));
}
console.log('Command still running: '+id);
process.exitCode = 1;
