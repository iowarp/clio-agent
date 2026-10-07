import { readFile, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { basename, resolve } from 'node:path';

// Run from the capture workspace after render-ffmpeg.mjs. Captions and posters
// describe the encoded native footage; no screenshots become video sources.
const [manifestPath, mediaDirectory] = process.argv.slice(2);
if (!manifestPath || !mediaDirectory) throw new Error('Supply edit manifest and media directory');
const edit = JSON.parse(await readFile(manifestPath, 'utf8'));
if (!/^[a-z0-9-]+$/.test(edit.name)) throw new Error('Invalid delivery name');
const segmentsDirectory = resolve('out', manifestPath.replace(/\.json$/, '') + '-segments');
const run = (program, args) => {
  const result = spawnSync(program, args, { encoding: 'utf8' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr || `${program} failed`);
  return result.stdout.trim();
};
const timestamp = seconds => new Date(Math.round(seconds * 1000)).toISOString().slice(11, 23);
let time = 0;
const cues = ['WEBVTT', ''];
for (const [index, shot] of edit.shots.entries()) {
  const duration = Number(run('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of',
    'default=nw=1:nk=1', resolve(segmentsDirectory, `${index}.mp4`)]));
  if (!(duration > 0)) throw new Error(`Invalid encoded segment ${index}`);
  cues.push(`${timestamp(time)} --> ${timestamp(time + duration)}`, shot.caption, '');
  time += duration;
}
const video = resolve(mediaDirectory, `${edit.name}.mp4`);
await writeFile(resolve(mediaDirectory, `${edit.name}.vtt`), cues.join('\n'));
run('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', '-threads', '1', '-ss',
  String(Math.max(0, Math.min(edit.posterAt ?? time - 2, time - 1))), '-i', video,
  '-filter_threads', '1', '-frames:v', '1', '-q:v', '2', '-threads', '1', resolve(mediaDirectory, `${edit.name}.jpg`)]);
console.log(JSON.stringify({ name: edit.name, seconds: time, manifest: basename(manifestPath) }));
