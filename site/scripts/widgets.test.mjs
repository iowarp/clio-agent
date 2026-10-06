import { describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import sharp from 'sharp';
import { widgets } from '../src/data/widget-library.mjs';
import { createThermalSpecimen } from './thermal-specimen.mjs';

function specimenFixture() {
  const binary = Buffer.alloc(36);
  [0, 0, 0, 0, 1, 0, 0, 2, 0].forEach((value, index) => binary.writeFloatLE(value, index * 4));
  const json = Buffer.from(JSON.stringify({ buffers: [{ byteLength: 36 }], bufferViews: [{ buffer: 0, byteLength: 36 }], accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3' }], meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }], scenes: [{ extras: {} }] }));
  const padded = Buffer.alloc(Math.ceil(json.length / 4) * 4, 0x20); json.copy(padded);
  const glb = Buffer.alloc(28 + padded.length + binary.length);
  glb.write('glTF'); glb.writeUInt32LE(2, 4); glb.writeUInt32LE(glb.length, 8);
  glb.writeUInt32LE(padded.length, 12); glb.write('JSON', 16); padded.copy(glb, 20);
  const start = 20 + padded.length;
  glb.writeUInt32LE(binary.length, start); glb.write('BIN\0', start + 4); binary.copy(glb, start + 8);
  return glb;
}

describe('native widget docs', () => {
  it('shows the exact agent fragment on component pages without rewritten field disclosures', () => {
    const detail = readFileSync(resolve('src/components/WidgetDetail.astro'), 'utf8');
    expect(detail).toContain('component-skills.json');
    expect(detail).toContain('{fragment.call}');
    expect(detail).toContain('{fragment.content}');
    expect(detail).not.toContain('Show {remaining} more fields');
    expect(detail).not.toContain('Browse all components');
  });
  it('stages complete agent fragments through the production resolver', () => {
    const directory = mkdtempSync(resolve(tmpdir(), 'clio-agent-fragments-'));
    try {
      for (const path of ['clio-workspace/v1', 'basic']) {
        mkdirSync(resolve(directory, path), { recursive: true });
        writeFileSync(resolve(directory, path, 'catalog.json'), JSON.stringify({ components: { [`test.${path}`]: { properties: { value: { $ref: '#/$defs/Value' } } } }, $defs: { Value: { type: 'number', description: 'Exact source description.' } } }));
      }
      const target = resolve(directory, 'output.json');
      const result = spawnSync('uv', ['run', '--no-project', 'python', 'scripts/render-agent-component-skills.py', directory, target], { encoding: 'utf8' });
      expect(result.status, result.stderr).toBe(0);
      const staged = JSON.parse(readFileSync(target, 'utf8'));
      expect(staged['test.clio-workspace/v1'].call).toBe('load_skill("a2ui-catalog-clio-workspace", file="catalog.json#/components/test.clio-workspace~1v1")');
      expect(JSON.parse(staged['test.clio-workspace/v1'].content)).toEqual({ properties: { value: { type: 'number', description: 'Exact source description.' } } });
      expect(staged['test.basic'].content).toBe(staged['test.clio-workspace/v1'].content);
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
  it('encodes a cropped recording with a visible pointer guide and matching poster', async () => {
    const directory = mkdtempSync(resolve(tmpdir(), 'clio-recording-'));
    try {
      await sharp({ create: { width: 80, height: 60, channels: 3, background: '#f4f9fa' } }).jpeg().toFile(resolve(directory, 'frame-00000.jpg'));
      await sharp({ create: { width: 80, height: 60, channels: 3, background: '#f4f9fa' } }).png().toFile(resolve(directory, 'frame-00001.jpg'));
      writeFileSync(resolve(directory, 'edit.json'), JSON.stringify({ width: 64, height: 48, crop: { x: 0, y: 0, width: 64, height: 48 }, poster: 1, posterCrop: { x: 16, y: 12, width: 64, height: 48 }, stages: [{ frames: [0], seconds: 0.5, caption: 'Recorded action', pointer: [[5, 5], [15, 10]] }, { frames: [1], seconds: 2, caption: 'Actual answer', crop: { x: 16, y: 12, width: 64, height: 48 } }] }));
      const result = spawnSync(process.execPath, ['scripts/encode-widget-recording.mjs', directory, resolve(directory, 'edit.json'), resolve(directory, 'output')], { encoding: 'utf8' });
      expect(result.status, result.stderr).toBe(0);
      expect(readFileSync(resolve(directory, 'output.mp4')).length).toBeGreaterThan(0);
      const poster = await sharp(resolve(directory, 'output.jpg')).metadata();
      expect([poster.width, poster.height]).toEqual([64, 48]);
      expect(readFileSync(resolve(directory, 'output.vtt'), 'utf8')).toContain('00:00:00.000 --> 00:00:00.500');
      const probe = spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'json', resolve(directory, 'output.mp4')], { encoding: 'utf8' });
      expect(probe.status, probe.stderr).toBe(0);
      expect(Number(JSON.parse(probe.stdout).format.duration)).toBeCloseTo(2.5, 1);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
  it('gives every catalog component a unique page, including both sliders', () => {
    expect(new Set(widgets.map((widget) => widget.slug)).size).toBe(widgets.length);
    expect(widgets.find((widget) => widget.name === 'Slider').slug).toBe('slider');
    expect(widgets.find((widget) => widget.name === 'clio.slider.v1').slug).toBe('numeric-slider');
  });

  it('preserves geometry and writes finite frame-major thermal values', () => {
    const source = specimenFixture();
    const output = createThermalSpecimen(source);
    const length = output.readUInt32LE(12);
    const document = JSON.parse(output.subarray(20, 20 + length).toString());
    const clio = document.scenes[0].extras.clio;
    expect(clio.frames).toHaveLength(61);
    expect(clio.frames[60].label).toBe('60 s');
    expect(clio.fields[0]).toMatchObject({ frames: 61, count: 3, unit: '°C' });
    const binary = output.subarray(28 + length);
    expect([...binary.subarray(0, 36)]).toEqual([...source.subarray(source.length - 36)]);
    const values = Array.from({ length: 183 }, (_, index) => binary.readFloatLE(36 + index * 4));
    expect(values.every((value) => Number.isFinite(value) && value >= 20 && value <= 120)).toBe(true);
    expect(values[0]).toBe(120);
    expect(values[180]).toBeLessThan(values[0]);
    expect(values[181]).toBeGreaterThan(values[1]);
    expect(output.readUInt32LE(8)).toBe(output.length);
  });

  it('rejects non-GLB input', () => {
    expect(() => createThermalSpecimen(Buffer.from('not a GLB'))).toThrow('Expected a GLB');
  });

  it('prepares repeatedly and restores the checkout exactly', () => {
    const root = mkdtempSync(resolve(tmpdir(), 'clio-docs-build-'));
    const web = resolve(root, 'web');
    mkdirSync(resolve(web, 'src/hooks'), { recursive: true });
    mkdirSync(resolve(web, 'src/components/clio'), { recursive: true });
    const originals = {
      'vite.config.ts': "const input = galleryOnly ? { gallery: fileURLToPath(new URL('./widget-preview.html', import.meta.url)) } : { gallery: fileURLToPath(new URL('./widget-preview.html', import.meta.url)), };",
      'src/hooks/use-repository.ts': "import { createGalleryRepository } from '@/lib/gallery-repository';\nreturn window.location.pathname.endsWith('/widget-preview.html') ? createGalleryRepository(repository) : repository;",
      'src/widget-preview.tsx': "function HurricaneShowcase() {}\nfunction IntroShowcase() {}\ncreateRoot(document.getElementById('root')!).render(view);",
      'src/gallery-skill-dialog.tsx': 'original skill module',
      'src/components/clio/use-chart-view.ts': '// Vega lays legends outside the plot width. Leave room inside the\n// surface so categorical and continuous legends remain readable.\nwidth: Math.max(220, measuredWidth - (colorField || seriesLegend.visible ? 112 : 0)),',
      'src/components/clio/a2ui-mesh-viewport.tsx': '          {boxButton()}\n          {zoomButtons()}\n          <SurfaceToolbar capabilities={toolbarCapabilities} floating={false} />',
      'src/widget-gallery.tsx': '<button>Contract</button>',
    };
    const run = (script) => {
      const result = spawnSync(process.execPath, [resolve('scripts', script), root], { encoding: 'utf8' });
      expect(result.stderr, result.stdout).toBe('');
      expect(result.status).toBe(0);
    };
    try {
      for (const [file, text] of Object.entries(originals)) writeFileSync(resolve(web, file), text);
      run('prepare-widget-engine.mjs');
      expect(readFileSync(resolve(web, 'src/components/clio/use-chart-view.ts'), 'utf8')).toContain('width: Math.max(220, measuredWidth),');
      expect(readFileSync(resolve(web, 'src/components/clio/a2ui-mesh-viewport.tsx'), 'utf8')).toContain('flex shrink-0 items-center gap-0.5');
      expect(readFileSync(resolve(web, 'src/widget-gallery.tsx'), 'utf8')).toContain('>Skill</button>');
      const first = readFileSync(resolve(web, 'src/hooks/use-repository.ts'), 'utf8');
      run('prepare-widget-engine.mjs');
      expect(readFileSync(resolve(web, 'src/hooks/use-repository.ts'), 'utf8')).toBe(first);
      run('cleanup-widget-engine.mjs');
      for (const [file, text] of Object.entries(originals)) expect(readFileSync(resolve(web, file), 'utf8')).toBe(text);
    } finally { rmSync(root, { recursive: true }); }
  });
});
