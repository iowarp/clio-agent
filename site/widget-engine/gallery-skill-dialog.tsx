import { lazy, Suspense, useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';

const MarkdownText = lazy(async () => ({ default: (await import('@/components/ai-elements/markdown')).MarkdownText }));
type AgentFragment = { call: string; content: string };
const sources = {
  agent: ['base-agent/AGENT.md', 'base-agent/experts/base.md'],
  presentation: ['present-interactive-analysis.md'],
} as const;
const stripFrontmatter = (body: string) => body.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, '').trim();

/** Show shipped Markdown and the exact output of the production load_skill resolver. */
export function GallerySkillDialog({ name, onClose }: { name: string | null; onClose: () => void }) {
  const general = name === 'general';
  const [section, setSection] = useState<keyof typeof sources>('agent');
  const [documents, setDocuments] = useState<Record<string, string>>({});
  const [fragments, setFragments] = useState<Record<string, AgentFragment>>({});
  const [error, setError] = useState('');
  useEffect(() => { setError(''); }, [name, section]);
  useEffect(() => {
    if (!name || (general ? documents[section] : fragments[name])) return;
    const controller = new AbortController();
    const read = async (path: string) => {
      const response = await fetch(`/widgets/gallery-skills/${path}`, { signal: controller.signal });
      if (!response.ok || response.headers.get('content-type')?.includes('text/html')) throw new Error(`Skill could not be loaded (${response.status}).`);
      return response;
    };
    const request = general
      ? Promise.all(sources[section].map(async (path) => stripFrontmatter(await (await read(path)).text()))).then((bodies) => setDocuments((previous) => ({ ...previous, [section]: bodies.join('\n\n---\n\n') })))
      : read('component-skills.json').then((response) => response.json()).then((data: Record<string, AgentFragment>) => {
        if (!data[name]) throw new Error(`No shipped skill found for ${name}.`);
        setFragments(data);
      });
    request.catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Skill could not be loaded.');
    });
    return () => controller.abort();
  }, [name, general, section, documents, fragments]);
  const fragment = fragments[name ?? ''];
  return <Dialog open={name !== null} onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent className={`gallery-skill-dialog widget-skill-dialog ${general ? '' : 'widget-skill-raw'}`}>
    <DialogHeader><DialogTitle>{general ? 'Agent and presentation skills' : `${name} skill`}</DialogTitle><DialogDescription>{general ? 'The shipped instructions used by CLIO.' : 'The exact component content returned to the agent by load_skill, including resolved local definitions.'}</DialogDescription></DialogHeader>
    {general && <div className="widget-skill-tabs" role="tablist" aria-label="Skill documents">{Object.keys(sources).map((id) => <button key={id} type="button" role="tab" aria-selected={section === id} onClick={() => setSection(id as keyof typeof sources)}>{id === 'agent' ? 'Standard agent' : 'Presentation skill'}</button>)}</div>}
    <div className="widget-skill-body">
      {general ? <Suspense fallback={<p>Loading skill…</p>}><MarkdownText mode="static">{error || documents[section] || 'Loading skill…'}</MarkdownText></Suspense> : fragment ? <><pre><code>{fragment.call}</code></pre><pre className="widget-agent-output"><code>{fragment.content}</code></pre></> : <p role="status">{error || 'Loading skill…'}</p>}
    </div>
  </DialogContent></Dialog>;
}
