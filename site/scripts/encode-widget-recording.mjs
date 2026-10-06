import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import sharp from 'sharp';

// Encode browser-captured frames only. The edit describes which genuine frames
// to keep and how long to show each stage; it never fabricates UI or responses.
const [sourceArgument, editArgument, outputArgument] = process.argv.slice(2);
if (!sourceArgument || !editArgument || !outputArgument) {
  throw new Error('Usage: node encode-widget-recording.mjs frames edit.json output-name');
}
const source = resolve(sourceArgument);
const output = resolve(outputArgument);
const edit = JSON.parse(await readFile(resolve(editArgument), 'utf8'));
const width = edit.width ?? 1280;
const height = edit.height ?? 720;
if (![width, height].every((value) => Number.isInteger(value) && value > 0 && value % 2 === 0)) throw new Error('Output dimensions must be positive even integers.');
const crop = edit.crop;
const filter = [`scale=${width}:${height}:force_original_aspect_ratio=decrease`, `pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2:color=0xf4f9fa`, 'setsar=1'].join(',');
const ffmpeg = process.env.FFMPEG_PATH || 'ffmpeg';
const escapePath = (path) => path.replaceAll('\\', '/').replaceAll("'", "'\\''");
const stamp = (seconds) => {
  const milliseconds = Math.round(seconds * 1000);
  return `${String(Math.floor(milliseconds / 3600000)).padStart(2, '0')}:${String(Math.floor(milliseconds / 60000) % 60).padStart(2, '0')}:${String(Math.floor(milliseconds / 1000) % 60).padStart(2, '0')}.${String(milliseconds % 1000).padStart(3, '0')}`;
};
let elapsed = 0;
const list = [];
const captions = ['WEBVTT\n'];
let frameSequence = 0;
const normalized = new Map();
const annotated = resolve(source, 'annotated');
await mkdir(annotated, { recursive: true });
for (const stage of edit.stages) {
  if (!Array.isArray(stage.frames) || !stage.frames.length || !(stage.seconds > 0)) throw new Error('Each stage needs frames and a positive duration.');
  const steps = stage.pointer ? Math.max(stage.frames.length, Math.ceil(stage.seconds * 15)) : stage.frames.length;
  for (let step = 0; step < steps; step += 1) {
    const index = stage.frames[Math.min(stage.frames.length - 1, Math.floor(step / steps * stage.frames.length))];
    const path = resolve(source, `frame-${String(index).padStart(5, '0')}.jpg`);
    await readFile(path); // Fail before encoding if a take is incomplete.
    // Screenshot bytes may be PNG even when the take uses .jpg filenames.
    // Concat needs one image codec and time base across annotated and raw frames.
    const frameCrop = stage.crop ?? crop;
    const key = `${index}:${JSON.stringify(frameCrop)}`;
    let prepared = normalized.get(key);
    if (!prepared) {
      prepared = resolve(annotated, `source-${String(normalized.size).padStart(6, '0')}.jpg`);
      let sourceImage = sharp(path);
      if (frameCrop) sourceImage = sourceImage.extract({left: frameCrop.x, top: frameCrop.y, width: frameCrop.width, height: frameCrop.height});
      await sourceImage.resize(width, height, {fit: 'contain', background: '#f4f9fa'}).jpeg({ quality: 95 }).toFile(prepared);
      normalized.set(key, prepared);
    }
    if (stage.pointer) {
      // Editorial pointer guidance follows the recorded action's endpoints.
      // It is separate from the captured UI and never changes its contents.
      const progress = steps <= 1 ? 1 : step / (steps - 1);
      const [start, end = start] = stage.pointer;
      const x = Math.round(start[0] + (end[0] - start[0]) * progress);
      const y = Math.round(start[1] + (end[1] - start[1]) * progress);
      const cursor = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="30" height="38"><path d="M3 2 L3 28 L10 22 L16 35 L21 32 L15 20 L25 20 Z" fill="#102b33" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>');
      prepared = resolve(annotated, `frame-${String(frameSequence++).padStart(6, '0')}.jpg`);
      // Composite before cropping so gesture coordinates remain source coordinates.
      const withPointer = await sharp(path).composite([{ input: cursor, left: x, top: y }]).png().toBuffer();
      let pointerImage = sharp(withPointer);
      if (frameCrop) pointerImage = pointerImage.extract({left: frameCrop.x, top: frameCrop.y, width: frameCrop.width, height: frameCrop.height});
      await pointerImage.resize(width, height, {fit: 'contain', background: '#f4f9fa'}).jpeg({ quality: 95 }).toFile(prepared);
    }
    list.push(`file '${escapePath(prepared)}'`, `duration ${stage.seconds / steps}`);
  }
  captions.push(`${stamp(elapsed)} --> ${stamp(elapsed + stage.seconds)}\n${stage.caption}\n`);
  elapsed += stage.seconds;
}
const last = edit.stages.at(-1).frames.at(-1);
list.push(`file '${escapePath(normalized.get(`${last}:${JSON.stringify(edit.stages.at(-1).crop ?? crop)}`))}'`);
await mkdir(resolve(output, '..'), { recursive: true });
const concat = resolve(source, 'edited.ffconcat');
await writeFile(concat, list.join('\n'));
const run = (arguments_) => {
  const result = spawnSync(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-y', ...arguments_], { stdio: 'inherit' });
  if (result.status !== 0) throw new Error(`ffmpeg failed: ${result.status}`);
};
run(['-f', 'concat', '-safe', '0', '-i', concat, '-vf', `${filter},fps=30`, '-t', String(elapsed), '-c:v', 'libx264', '-crf', '20', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', `${output}.mp4`]);
const posterIndex = edit.poster ?? edit.stages[0].frames[0];
const posterCrop = edit.posterCrop ?? crop;
let posterImage = sharp(resolve(source, `frame-${String(posterIndex).padStart(5, '0')}.jpg`));
if (posterCrop) posterImage = posterImage.extract({left: posterCrop.x, top: posterCrop.y, width: posterCrop.width, height: posterCrop.height});
await posterImage.resize(width, height, {fit: 'contain', background: '#f4f9fa'}).jpeg({quality: 95}).toFile(`${output}.jpg`);
await writeFile(`${output}.vtt`, captions.join('\n'));
console.log(`Encoded ${elapsed.toFixed(1)} seconds in ${output}.mp4`);
