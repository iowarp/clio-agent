import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import sharp from 'sharp';

const timeline = JSON.parse(readFileSync('video/source-marketplace-guides.edit.json', 'utf8'));
const pages = ['files', 'marketplaces'].flatMap((directory) =>
  readdirSync(`src/content/docs/docs/${directory}`).filter((name) => name.endsWith('.mdx'))
    .map((name) => readFileSync(`src/content/docs/docs/${directory}/${name}`, 'utf8')),
);

describe('published source and marketplace walkthroughs', () => {
  it('ships every referenced image, video, poster, and caption file', async () => {
    for (const page of pages) {
      for (const match of page.matchAll(/<GuideScreenshot\s+name="([^"]+)"/g)) {
        expect((await sharp(`public/media/${match[1]}.jpg`).metadata()).width).toBeGreaterThan(300);
      }
      for (const match of page.matchAll(/<WidgetRecording\s+name="([^"]+)"[^>]+\/>/g)) {
        const name = match[1];
        expect(match[0]).toContain('formatLabel="Screenshot walkthrough"');
        expect(readFileSync(`public/media/${name}.mp4`).subarray(4, 8).toString()).toBe('ftyp');
        const poster = await sharp(`public/media/${name}.jpg`).metadata();
        expect([poster.width, poster.height]).toEqual(timeline.size);
        expect(readFileSync(`public/media/${name}.vtt`, 'utf8')).toMatch(/^WEBVTT/);
      }
    }
  });

  it('preserves caption coverage for every shot in the editable timeline', () => {
    for (const clip of timeline.clips) {
      const vtt = readFileSync(`public/media/${clip.name}.vtt`, 'utf8');
      expect(vtt.match(/ --> /g)).toHaveLength(clip.shots.length);
      for (const shot of clip.shots) {
        expect(vtt).toContain(shot.caption);
        expect(shot.seconds).toBeGreaterThanOrEqual(4);
      }
    }
  });
});
