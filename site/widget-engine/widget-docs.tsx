import { useEffect, useState, type PropsWithChildren, type ReactNode } from 'react';
import { useTheme } from 'next-themes';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AppProviders } from '@/providers/app-providers';
import { A2uiDemo, WidgetGallery } from '@/widget-gallery';
import { GallerySkillDialog } from '@/gallery-skill-dialog';
import { HurricaneShowcase } from '@/widget-preview';
import { SimulationExample } from '@/simulation-example';
import { ArrowUpRight } from 'lucide-react';
import { SelectionActionsContext } from '@/lib/selection-actions-context';
import { createSelectionActionRegistry, type DataSurfaceZoneSelection } from '@/lib/selection-actions';
import { useMemo } from 'react';
import './index.css';
import './widget-gallery.css';

/** Pass Starlight's theme to the workspace provider without observing its output. */
function DocsThemeBridge({ children }: PropsWithChildren) {
  const { setTheme } = useTheme();
  useEffect(() => {
    const sync = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, [setTheme]);
  return children;
}

const chartVariants = ['scatter', 'boxplot', 'heatmap', 'trajectories', 'storm-series', 'spectra'];
const mapVariants = [{ id: 'sites', label: 'Sites' }, { id: 'storm-tracks', label: 'Storm tracks' }];

const introExamples = [
  { id: 'model', label: '3D field', eyebrow: 'Data in three dimensions', title: 'Explore the result from every angle', prompt: 'Where does this specimen carry the highest relative load?', description: 'Orbit the model and probe the field behind the answer.', name: 'clio.mesh-viewport.v1', variant: 'intro-load', href: '/docs/widgets/mesh-viewport/' },
  { id: 'map', label: 'Storm tracks', eyebrow: 'Data in space', title: 'Follow a changing path', prompt: 'How did these three storm tracks move across the Atlantic?', description: 'Trace each route and inspect its observations on the map.', name: 'clio.map.v1', variant: 'storm-tracks', href: '/docs/widgets/map/' },
  { id: 'chart', label: 'Measurements', eyebrow: 'Data in relationships', title: 'Find the shape of the data', prompt: 'Which observations sit outside the main cluster?', description: 'Compare values, inspect individual points, and carry a selection forward.', name: 'clio.chart.v1', variant: 'scatter', href: '/docs/widgets/chart/' },
] as const;

function IntroExamples() {
  const [active, setActive] = useState<(typeof introExamples)[number]['id']>('model');
  const example = introExamples.find((item) => item.id === active)!;
  return <section className="widget-intro-examples" aria-label="Interactive widget examples">
    <div className="widget-intro-example-tabs" role="tablist" aria-label="Explore a CLIO view">
      {introExamples.map((item) => <button key={item.id} type="button" role="tab" aria-selected={active === item.id} onClick={() => setActive(item.id)}>{item.label}</button>)}
    </div>
    <div className="widget-intro-example-body" role="tabpanel">
      <div className="widget-intro-example-surface"><A2uiDemo key={example.id} name={example.name} variant={example.variant} /></div>
      <div className="widget-intro-example-copy"><span className="widget-eyebrow">{example.eyebrow}</span><h2>{example.title}</h2><p className="widget-intro-question">“{example.prompt}”</p><p>{example.description}</p><a href={example.href}>Explore the component <span aria-hidden="true">↗</span></a></div>
    </div>
  </section>;
}

function ComponentExample({ name }: { name: string }) {
  const [variant, setVariant] = useState(name === 'clio.mesh-viewport.v1' ? 'intro-load' : name === 'clio.map.v1' ? 'storm-tracks' : 'scatter');
  const [skill, setSkill] = useState<string | null>(null);
  return <div className="space-y-4">
    <div className="flex justify-end"><button type="button" className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-muted" onClick={() => setSkill(name)}>Component skill</button></div>
    {name === 'clio.chart.v1' && <div className="gallery-tab-scroll flex gap-1 overflow-x-auto" role="group" aria-label="Chart example">
      {chartVariants.map((item) => <button key={item} type="button" aria-pressed={variant === item} className={`shrink-0 rounded-md px-3 py-1.5 text-xs capitalize ${variant === item ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}`} onClick={() => setVariant(item)}>{item === 'storm-series' ? 'Storm series' : item}</button>)}
    </div>}
    {name === 'clio.map.v1' && <div className="gallery-tab-scroll flex gap-1 overflow-x-auto" role="group" aria-label="Map example">
      {mapVariants.map((item) => <button key={item.id} type="button" aria-pressed={variant === item.id} className={`shrink-0 rounded-md px-3 py-1.5 text-xs ${variant === item.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}`} onClick={() => setVariant(item.id)}>{item.label}</button>)}
    </div>}
    {name === 'clio.mesh-viewport.v1' && <div className="gallery-tab-scroll flex gap-1 overflow-x-auto" role="group" aria-label="3D example">
      {[{ id: 'intro-load', label: 'Load field' }, { id: 'surface', label: 'Terrain mesh' }].map((item) => <button key={item.id} type="button" aria-pressed={variant === item.id} className={`shrink-0 rounded-md px-3 py-1.5 text-xs ${variant === item.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}`} onClick={() => setVariant(item.id)}>{item.label}</button>)}
    </div>}
    <A2uiDemo key={`${name}:${variant}`} name={name} variant={variant} />
    <GallerySkillDialog name={skill} onClose={() => setSkill(null)} />
  </div>;
}

function GalleryWithSkills() {
  const [skill, setSkill] = useState<string | null>(null);
  useEffect(() => {
    const button = document.querySelector<HTMLButtonElement>('[data-gallery-skill-open]');
    const open = () => setSkill('general');
    button?.addEventListener('click', open);
    return () => button?.removeEventListener('click', open);
  }, []);
  return <div><WidgetGallery /><GallerySkillDialog name={skill} onClose={() => setSkill(null)} /></div>;
}

function LinkedExamples() {
  const [active, setActive] = useState<'hurricanes' | 'simulation'>('hurricanes');
  const hurricane = active === 'hurricanes';
  return <section aria-label="Linked data examples"><div className="widget-detail-tablist" role="tablist" aria-label="Linked data examples">
    {[{ id: 'hurricanes', label: 'Hurricane tracks' }, { id: 'simulation', label: 'Simulation over time' }].map((item) => <button key={item.id} type="button" role="tab" aria-selected={active === item.id} onClick={() => setActive(item.id as typeof active)}>{item.label}</button>)}
  </div><div role="tabpanel"><DocsReferenceProvider key={active}>{(preview) => <>
    <div className="widget-linked-prompt"><blockquote>“{hurricane ? 'How did Harvey, Maria, and Dorian move, and when did each reach its strongest winds?' : 'How does the hot region spread through the specimen as it cools?'}”</blockquote><p>{hurricane ? 'Pick an observation in any view to find it in the others.' : 'Move the time slider and orbit either model to compare the evolving field with its initial state.'}</p></div>
    {hurricane ? <><HurricaneShowcase />{preview}</> : <SimulationExample>{preview}</SimulationExample>}
    <div className="widget-linked-reveal"><h2>What the views reveal</h2><p>{hurricane ? 'The tracks show where each storm travelled. The wind chart shows when its intensity changed; the table keeps the exact time, position, and wind speed. One selection connects an observation across all three views.' : 'A time slider changes the temperature field while the initial state stays beside it. Orbiting either model moves both cameras, so the comparison keeps the same angle. Linking can coordinate parameters and cameras as well as selected records.'}</p></div>
  </>}</DocsReferenceProvider></div></section>;
}

function InteractionExamples({ name }: { name?: string }) {
  const [active, setActive] = useState<'data' | 'visual'>('data');
  if (name) return <A2uiDemo name={name === 'data' ? 'clio.map.v1' : 'clio.mesh-viewport.v1'} variant={name === 'data' ? 'storm-tracks' : 'intro-load'} />;
  return <section className="widget-intro-examples" aria-label="Try selection and capture">
    <div className="widget-intro-example-tabs widget-interaction-tabs" role="tablist" aria-label="Interaction examples">
      <button type="button" role="tab" aria-selected={active === 'data'} onClick={() => setActive('data')}>Select and reference</button>
      <button type="button" role="tab" aria-selected={active === 'visual'} onClick={() => setActive('visual')}>Mark a visual region</button>
    </div>
    <div className="widget-intro-example-surface" role="tabpanel">
      <p className="widget-example-provenance">{active === 'data' ? 'Click a point to select it. Ctrl-click selects its trajectory; Shift-click adds or removes points. Then choose Reference this.' : 'Open Capture labelled regions in the toolbar, draw a box, add a comment, and add the region to the preview.'}</p>
      <A2uiDemo key={active} name={active === 'data' ? 'clio.map.v1' : 'clio.mesh-viewport.v1'} variant={active === 'data' ? 'storm-tracks' : 'intro-load'} />
    </div>
  </section>;
}

/** Give every standalone docs example a real destination for its reference action. */
function DocsReferenceProvider({ children }: { children: ReactNode | ((preview: ReactNode) => ReactNode) }) {
  const actions = useMemo(() => createSelectionActionRegistry(), []);
  const [reference, setReference] = useState<DataSurfaceZoneSelection>();
  const [capture, setCapture] = useState<{ url: string; text: string }>();
  useEffect(() => actions.register({
    id: 'docs-reference', label: 'Reference this', icon: ArrowUpRight, order: 10,
    kinds: ['data-surface-zone'], run: (target) => { if (target.kind === 'data-surface-zone') setReference(target); },
  }), [actions]);
  useEffect(() => {
    const show = (event: Event) => {
      const detail = (event as CustomEvent<{ file: File; text: string }>).detail;
      if (detail.file.name.startsWith('clio-capture-docs-')) setCapture({ url: URL.createObjectURL(detail.file), text: detail.text });
    };
    window.addEventListener('clio:add-region-capture', show);
    return () => window.removeEventListener('clio:add-region-capture', show);
  }, []);
  useEffect(() => () => { if (capture) URL.revokeObjectURL(capture.url); }, [capture]);
  const preview = <>{reference && <aside className="widget-reference-preview" aria-label="Reference preview"><header><strong>Ready for your next question</strong><button type="button" aria-label="Close reference preview" onClick={() => setReference(undefined)}>Close</button></header><p>{reference.summary}</p><details><summary>Inspect the data the agent receives</summary><pre>{reference.markdown}</pre></details><label>Your follow-up<textarea placeholder="What would you like to understand about this selection?" /></label><p className="widget-example-provenance">This docs preview shows the attachment. Sending a follow-up happens in your CLIO conversation.</p></aside>}{capture && <aside className="widget-reference-preview" aria-label="Capture preview"><header><strong>Visual context for your next question</strong><button type="button" onClick={() => setCapture(undefined)}>Close</button></header><img src={capture.url} alt="Captured view with labelled regions" /><details><summary>Inspect the image context the agent receives</summary><pre>{capture.text}</pre></details></aside>}</>;
  return <SelectionActionsContext.Provider value={actions}>{typeof children === 'function' ? children(preview) : <>{children}{preview}</>}</SelectionActionsContext.Provider>;
}

document.querySelectorAll<HTMLElement>('[data-clio-widget-live]').forEach((element) => {
  const mode = element.dataset.clioWidgetLive;
  const name = element.dataset.component;
  const view = mode === 'gallery' ? <GalleryWithSkills /> : mode === 'linked' ? <LinkedExamples /> : mode === 'intro' ? <IntroExamples /> : mode === 'interaction' ? <InteractionExamples name={name} /> : name ? <ComponentExample name={name} /> : null;
  if (view) createRoot(element).render(<BrowserRouter><AppProviders><DocsThemeBridge>{mode === 'gallery' || mode === 'linked' ? view : <DocsReferenceProvider>{view}</DocsReferenceProvider>}</DocsThemeBridge></AppProviders></BrowserRouter>);
});
