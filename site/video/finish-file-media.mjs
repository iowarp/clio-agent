import { readFile, writeFile, copyFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

// Delivery images and captions come from the real recordings and source screenshots.
function command(program, args) {
  const result = spawnSync(program, args, { encoding: 'utf8' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr);
  return result.stdout.trim();
}
function timestamp(seconds) {
  return new Date(Math.round(seconds * 1000)).toISOString().slice(11, 23);
}
for (const [name, posterAt] of [['files-pdf-read', 11], ['files-word-edit', 45], ['files-office-review', 77]]) {
  const edit = JSON.parse(await readFile(`${name}.edit.json`, 'utf8'));
  let time = 0;
  const cues = ['WEBVTT', ''];
  for (const [index, shot] of edit.shots.entries()) {
    const segment = resolve('out', `${name}.edit-segments`, `${index}.mp4`);
    const duration = Number(command('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', segment]));
    cues.push(`${timestamp(time)} --> ${timestamp(time + duration)}`, shot.caption, '');
    time += duration;
  }
  await writeFile(`../public/media/${name}.vtt`, cues.join('\n'));
  command('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', '-ss', String(Math.min(posterAt, time - 1)), '-i', `../public/media/${name}.mp4`, '-frames:v', '1', '-q:v', '2', `../public/media/${name}.jpg`]);
  console.log(JSON.stringify({ name, seconds: time }));
}
await copyFile('public/takes/files-reference-picker-raw.png', '../public/media/files-reference-picker.png');
await copyFile('public/takes/files-word-preview-final-raw.png', '../public/media/files-word-preview.png');
command('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', '-ss', '257', '-t', '7.7', '-i', 'public/takes/files-office-followup-20261005-e.mp4', '-filter_complex', 'fps=10,scale=960:-2:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse', '-loop', '0', '../public/media/files-slide-navigation.gif']);
