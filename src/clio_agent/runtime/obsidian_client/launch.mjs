// The official client's secrets are passed over stdin, never OS process arguments.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const request = JSON.parse(readFileSync(0, 'utf8'));
const entry = process.argv[2];
const args = [...request.args];
for (const [name, value] of Object.entries(request.secrets || {})) {
  if (value) args.push(`--${name}=${value}`);
}
process.argv = [process.execPath, entry, ...args];
await import(pathToFileURL(entry).href);
