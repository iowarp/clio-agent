import { copyFile, readdir, rm, stat } from 'node:fs/promises';
import { resolve } from 'node:path';

const web = resolve(process.argv[2] ?? '../external/gact-tui', 'web');
const backup = resolve(web, '.clio-docs-original');
if ((await stat(backup).catch(() => null))?.isDirectory()) {
  for (const file of await readdir(backup)) {
    await copyFile(resolve(backup, file), resolve(web, file.replaceAll('__', '/')));
  }
  await rm(backup, { recursive: true });
}
for (const file of ['widget-docs.html', 'src/widget-docs.tsx', 'src/docs-repository.ts', 'src/simulation-example.tsx', 'src/docs-widget-guidance.ts']) {
  await rm(resolve(web, file), { force: true });
}
