import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

/** Published native exports, pinned to the reviewed bytes rather than a moving session. */
export const ndpConversations = [
  { slug: 'alta', file: 'Alta-snowfall-session.html', bytes: 40359492,
    sha256: 'f1d5dd67cdf564f8619c86d5a7d407f39c38b7fed4a7b54d4beea09df1813f6e' },
  { slug: 'tahoe', file: 'Lake-Tahoe-session.html', bytes: 61345784,
    sha256: 'e26c03515dd3fb5b35a11522792731a2ecc29b6362040bc641cc7f03579723e3' },
  { slug: 'konza', file: 'Konza-session.html', bytes: 46724281,
    sha256: '42ed10fac4f83e365232403608d6f76ba34d6def3cb2ed7c190325dedbd1a1f8' },

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
