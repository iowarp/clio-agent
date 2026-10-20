import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

/** Published native exports, pinned to the reviewed bytes rather than a moving session. */
export const ndpConversations = [
  { slug: 'alta', file: 'Alta-snowfall-session-maps-v2.html', bytes: 40359592,
    sha256: '84e3de064bd91e453463f45c0179f34116c658bea5e90c7b088fa27a5a30e69f' },
  { slug: 'tahoe', file: 'Lake-Tahoe-session-maps-v2.html', bytes: 61345872,
    sha256: '58a93fa96435f2489307130565002f732cece325e0f7a5509c60a89cad709bfa' },
  { slug: 'konza', file: 'Konza-session-maps-v2.html', bytes: 46724377,
    sha256: 'd9653d6835153479e2395f0eae082c8b8b07388cfd93ba22e349a901c0104f93' },
  { slug: 'endpoint', file: 'Endpoint-workflow-session.html', bytes: 33013584,
    sha256: '682061c54d38863eba3deae981b12b142f204ca12f24880e0de0cacba50cb144' },
] as const;

interface ConversationAsset {
  readonly file: string;
  readonly bytes: number;
  readonly sha256: string;
}

const release = 'https://github.com/iowarp/clio-agent/releases/download/ndp-scientific-conversations-2026-10-19/';

function matches(asset: ConversationAsset, content: Uint8Array): boolean {
  return content.byteLength === asset.bytes &&
    createHash('sha256').update(content).digest('hex') === asset.sha256;
}

/** Fetch a public export during the static build; refuse changed or incomplete content. */
export async function readNdpConversation(
  asset: ConversationAsset,
  { cacheDirectory = resolve('node_modules/.cache/ndp-conversations'), fetchAsset = fetch }:
    { cacheDirectory?: string; fetchAsset?: typeof fetch } = {},
): Promise<Uint8Array> {
  if (!/^[A-Za-z0-9-]+\.html$/.test(asset.file))
    throw new Error('Invalid conversation filename');
  const cachePath = join(cacheDirectory, asset.file);
  try {
    const cached = await readFile(cachePath);
    if (matches(asset, cached)) return cached;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const response = await fetchAsset(release + asset.file, { signal: AbortSignal.timeout(180_000) });
  if (!response.ok) throw new Error(`Conversation download failed: ${asset.file} (HTTP ${response.status})`);
  const content = new Uint8Array(await response.arrayBuffer());
  if (!matches(asset, content)) throw new Error(`Conversation integrity check failed: ${asset.file}`);
  await mkdir(cacheDirectory, { recursive: true });
  const temporary = `${cachePath}.${process.pid}.tmp`;
  await writeFile(temporary, content);
  await rename(temporary, cachePath);
  return content;
}
