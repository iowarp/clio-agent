import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

/** One published native export, pinned to the reviewed bytes rather than a moving session. */
export interface PublishedConversation {
  readonly slug: string;
  readonly file: string;
  readonly bytes: number;
  readonly sha256: string;
}

/** A set of exports published together under one base URL (a release or a pinned commit). */
export interface ConversationCollection {
  readonly id: string;
  readonly release: string;
  readonly conversations: readonly PublishedConversation[];
}

const releases = 'https://github.com/iowarp/clio-agent/releases/download/';
// JaimeCernuda/clio-exports at a fixed commit: the files there never change under a URL.
const exports = 'https://raw.githubusercontent.com/JaimeCernuda/clio-exports/32bc650e38af894e7e74b5091b2842c855bec314/';

/** Every published conversation, served at /conversations/<collection>/<slug>/. */
export const conversationCollections: readonly ConversationCollection[] = [
  {
    id: 'ndp',
    release: `${releases}ndp-scientific-conversations-2026-10-19/`,
    conversations: [
      { slug: 'alta', file: 'Alta-snowfall-session-maps-v2.html', bytes: 40359592,
        sha256: '84e3de064bd91e453463f45c0179f34116c658bea5e90c7b088fa27a5a30e69f' },
      { slug: 'tahoe', file: 'Lake-Tahoe-session-maps-v2.html', bytes: 61345872,
        sha256: '58a93fa96435f2489307130565002f732cece325e0f7a5509c60a89cad709bfa' },
      { slug: 'konza', file: 'Konza-session-maps-v2.html', bytes: 46724377,
        sha256: 'd9653d6835153479e2395f0eae082c8b8b07388cfd93ba22e349a901c0104f93' },
      { slug: 'endpoint', file: 'Endpoint-workflow-session.html', bytes: 33013584,
        sha256: '682061c54d38863eba3deae981b12b142f204ca12f24880e0de0cacba50cb144' },
    ],
  },
  {
    // Plain-request sessions recorded with default delegation (too large to commit).
    id: 'delegation',
    release: `${exports}delegation/`,
    conversations: [
      { slug: 'reading-guide', file: 'Reading-guide-session.html', bytes: 87331887,
        sha256: 'bd505defbd060f243d7a7da315621fdc4e13d97a612d6a0f4e94c28c19d1837a' },
      { slug: 'skill-review', file: 'Skill-review-session.html', bytes: 35512587,
        sha256: '1c3622e3f8754e977b0557e800f6514dc4446f6d47827336ac6650f4b65aef9f' },
    ],
  },
  {
    // Branched studies asked as plain questions, recorded in the science log.
    id: 'studies',
    release: `${exports}studies/`,
    conversations: [
      { slug: 'wordle', file: 'Wordle-study-session.html', bytes: 48278832,
        sha256: '8d3a29db0ec2aa39fad261f78a017e520715c94317815ffb36ca4483c00d6b61' },
    ],
  },
];

function matches(asset: PublishedConversation, content: Uint8Array): boolean {
  return content.byteLength === asset.bytes &&
    createHash('sha256').update(content).digest('hex') === asset.sha256;
}

/** Fetch a public export during the static build; refuse changed or incomplete content. */
export async function readPublishedConversation(
  collection: Pick<ConversationCollection, 'id' | 'release'>,
  asset: PublishedConversation,
  { cacheDirectory = resolve('node_modules/.cache/published-conversations'), fetchAsset = fetch }:
    { cacheDirectory?: string; fetchAsset?: typeof fetch } = {},
): Promise<Uint8Array> {
  if (!/^[A-Za-z0-9-]+\.html$/.test(asset.file) || !/^[a-z0-9-]+$/.test(collection.id))
    throw new Error('Invalid conversation filename');
  const directory = join(cacheDirectory, collection.id);
  const cachePath = join(directory, asset.file);
  try {
    const cached = await readFile(cachePath);
    if (matches(asset, cached)) return cached;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const response = await fetchAsset(collection.release + asset.file, { signal: AbortSignal.timeout(180_000) });
  if (!response.ok) throw new Error(`Conversation download failed: ${asset.file} (HTTP ${response.status})`);
  const content = new Uint8Array(await response.arrayBuffer());
  if (!matches(asset, content)) throw new Error(`Conversation integrity check failed: ${asset.file}`);
  await mkdir(directory, { recursive: true });
  const temporary = `${cachePath}.${process.pid}.tmp`;
  await writeFile(temporary, content);
  await rename(temporary, cachePath);
  return content;
}
