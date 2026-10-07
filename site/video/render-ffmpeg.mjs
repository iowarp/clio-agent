import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {resolve, relative} from 'node:path';
import {spawnSync} from 'node:child_process';

// Low-memory video editing: decode the recorded MP4s, never source screenshots.
const [manifest, destination] = process.argv.slice(2);
if (!manifest || !destination) throw new Error('Usage: node render-ffmpeg.mjs edit.json out/video.mp4');
const edit = JSON.parse(await readFile(manifest, 'utf8'));
const directory = resolve('out', manifest.replace(/\.json$/, '') + '-segments');
await mkdir(directory, {recursive:true});
const segments = [];
function ffmpeg(args) {
  const result = spawnSync('ffmpeg', ['-y', '-hide_banner', '-loglevel', 'error', ...args], {encoding:'utf8'});
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr || `FFmpeg exited ${result.status}`);
}
for (const [index, shot] of edit.shots.entries()) {
  if (!(shot.to > shot.from)) throw new Error(`Invalid interval in shot ${index}`);
  const speed = shot.speed ?? 1;
  if (!Number.isFinite(speed) || speed <= 0) throw new Error(`Invalid speed in shot ${index}`);
  const crop = shot.crop ?? {x:0,y:0,width:1280,height:870};
  const filters = [`setpts=(PTS-STARTPTS)/${speed}`, `fps=30`, `crop=${crop.width}:${crop.height}:${crop.x}:${crop.y}`, 'scale=1280:870:force_original_aspect_ratio=decrease:in_range=auto:out_range=tv', 'pad=1280:870:(ow-iw)/2:(oh-ih)/2:color=0xf4f9f9', 'setsar=1'];
  if (shot.zoom) filters.push(`zoompan=z='1+${shot.zoom-1}*min(on/30,1)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1280x870:fps=30`);
  if (shot.caption) {
    const captionPath = resolve(directory, `${index}.txt`);
    await writeFile(captionPath, shot.caption, 'utf8');
    const portablePath = relative(process.cwd(), captionPath).replaceAll('\\','/');
    filters.push(`drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':textfile='${portablePath}':fontsize=21:fontcolor=white:box=1:boxcolor=0x063941ee:boxborderw=12:x=24:y=h-55`);
  }
  filters.push('format=yuv420p');
  const segment = resolve(directory, `${index}.mp4`);
  ffmpeg(['-threads','1','-ss',String(shot.from),'-t',String(shot.to-shot.from),'-i',resolve('public',shot.source ?? edit.source),'-an','-filter_threads','1','-vf',filters.join(','),'-c:v','libx264','-preset','fast','-crf','18','-threads','1','-color_range','tv',segment]);
  segments.push(segment);
  console.log(`Edited recorded video shot ${index+1}/${edit.shots.length}`);
}
const concatPath = resolve(directory, 'concat.txt');
await writeFile(concatPath, segments.map((path) => `file '${path.replaceAll('\\','/').replaceAll("'", "'\\''")}'`).join('\n'));
ffmpeg(['-f','concat','-safe','0','-i',concatPath,'-c','copy','-movflags','+faststart',resolve(destination)]);
console.log(`Saved ${destination}`);
