import type { createRepository } from '@/lib/connection';

/** Static docs assets consumed through the ordinary artifact API. */
export function createDocsRepository(base: ReturnType<typeof createRepository>) {
  return new Proxy(base, {
    get(target, property, receiver) {
      if (property === 'readArtifactBytes') return async (id: string, path?: string, signal?: AbortSignal) => {
        if (id !== 'artifact_docs_thermal_specimen') return target.readArtifactBytes(id, path, signal);
        const response = await fetch('/widgets/gallery/thermal-specimen.glb', { signal });
        if (!response.ok) throw new Error(`Thermal specimen unavailable (${response.status}).`);
        return new Uint8Array(await response.arrayBuffer());
      };
      const value: unknown = Reflect.get(target, property, receiver);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}
