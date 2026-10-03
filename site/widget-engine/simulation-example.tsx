import { useMemo, type PropsWithChildren } from 'react';
import { renderMarkdown } from '@a2ui/markdown-it';
import { MarkdownContext } from '@a2ui/react/v0_9';
import { Catalog, MessageProcessor, type A2uiMessage } from '@a2ui/web_core/v0_9';
import { A2uiSurface, KERNEL_COMPONENTS, KERNEL_FUNCTIONS } from '@/lib/a2ui/kernel-catalog';
import { A2uiReferenceSessionProvider } from '@/lib/a2ui/reference-session';
import { A2uiRegionCaptureProvider } from '@/components/clio/a2ui-region-capture';
import { CLIO_WORKSPACE_CATALOG_ROW } from '@/test-fixtures/a2ui/v0_9_1/fixtures';

const components = [
  { id: 'root', component: 'Column', children: ['time', 'views'] },
  { id: 'time', component: 'clio.slider.v1', label: 'Elapsed time', value: { path: '/thermal/time' }, min: 0, max: 60, step: 1, unit: 's' },
  { id: 'views', component: 'Grid', columns: 2, children: ['initial', 'current'] },
  { id: 'initial', component: 'clio.mesh-viewport.v1', title: 'Initial temperature', meshUri: 'artifact://artifact_docs_thermal_specimen', format: 'glb', field: 'temperature', frame: 0, syncGroup: 'docs-thermal' },
  { id: 'current', component: 'clio.mesh-viewport.v1', title: 'Temperature at selected time', meshUri: 'artifact://artifact_docs_thermal_specimen', format: 'glb', field: 'temperature', frame: { path: '/thermal/time' }, syncGroup: 'docs-thermal' },
];

/** One bound slider drives a multiframe field; both views share camera state. */
export function SimulationExample({ children }: PropsWithChildren) {
  const surface = useMemo(() => {
    const catalogId = CLIO_WORKSPACE_CATALOG_ROW.catalogId;
    const processor = new MessageProcessor([new Catalog(catalogId, [...KERNEL_COMPONENTS.values()], [...KERNEL_FUNCTIONS.values()])], async () => undefined, { version: 'v0.9.1' });
    const surfaceId = 'docs-thermal-evolution';
    processor.processMessages([
      { version: 'v0.9.1', createSurface: { surfaceId, catalogId } },
      { version: 'v0.9.1', updateDataModel: { surfaceId, path: '/thermal/time', value: 20 } },
      { version: 'v0.9.1', updateComponents: { surfaceId, components } },
    ] as A2uiMessage[]);
    return processor.model.getSurface(surfaceId)!;
  }, []);
  return <section className="widget-simulation" aria-label="Thermal simulation example">
    <div data-slot="a2ui-surface-root"><MarkdownContext.Provider value={renderMarkdown}><A2uiReferenceSessionProvider value="sess_flat_ndp"><A2uiRegionCaptureProvider allowDemoCapture surface={{ id: surface.id, revision: 1, messages: [{ updateComponents: { components } }] }}><A2uiSurface surface={surface} /></A2uiRegionCaptureProvider></A2uiReferenceSessionProvider></MarkdownContext.Provider></div>
    {children}
    <p className="widget-example-provenance">Illustrative temperature field from the analytical one-dimensional heat equation, sampled at 61 times. Both views use the same temperature scale. This is a teaching dataset, not a measured or validated engineering result.</p>
    <details className="widget-link-explanation"><summary>How the agent connects these components</summary><p>The slider writes to <code>/thermal/time</code>. The second viewport reads that path as its frame number. Both viewports read one registered mesh and use the same camera synchronization group. These are ordinary catalog components and data bindings.</p></details>
  </section>;
}
