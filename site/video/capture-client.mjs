import {writeFile, rename, readFile, unlink} from 'node:fs/promises';
import {resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

/** Submit one command to the running continuous recorder and await its reply. */
export async function sendCommand(action) {
  const stem=resolve('.capture-commands',`${Date.now()}-${randomUUID()}`);
  await writeFile(stem+'.pending',JSON.stringify(action));
  await rename(stem+'.pending',stem+'.command.json');
  for(let attempt=0;attempt<600;attempt++) {
    try {
      const reply=JSON.parse(await readFile(stem+'.reply.json','utf8'));
      await unlink(stem+'.reply.json');
      if(reply.error) throw new Error(reply.error);
      return reply;
    } catch(error) {
      // The recorder may still be writing its reply when this poll reads it.
      if(error.code!=='ENOENT' && !(error instanceof SyntaxError)) throw error;
    }
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  throw new Error('Recorder command timed out; inspect the running recorder before retrying');
}
