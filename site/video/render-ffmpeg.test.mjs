import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {test} from 'node:test';

test('accelerated continuous footage has the expected output duration', async () => {
  const directory = await mkdtemp(resolve(tmpdir(), 'clio-video-speed-'));
  await mkdir(resolve(directory, 'public'));
  const fixture = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
    'testsrc2=size=1280x870:rate=30:duration=3', '-c:v', 'libx264', '-threads', '1',
    resolve(directory, 'public/source.mp4')], {encoding:'utf8'});
  assert.equal(fixture.status, 0, fixture.stderr);
  await writeFile(resolve(directory, 'edit.json'), JSON.stringify({source:'source.mp4',
    shots:[{from:0,to:3,speed:3}]}));
  const result = spawnSync(process.execPath, [resolve('render-ffmpeg.mjs'), 'edit.json', 'edited.mp4'],
    {cwd:directory,encoding:'utf8'});
  assert.equal(result.status, 0, result.stderr);
  const probe = spawnSync('ffprobe', ['-v','error','-show_entries','format=duration',
    '-of','json',resolve(directory,'edited.mp4')], {encoding:'utf8'});
  assert.equal(probe.status, 0, probe.stderr);
  assert.ok(Math.abs(Number(JSON.parse(probe.stdout).format.duration)-1) < 0.04);
  await writeFile(resolve(directory,'invalid.json'), JSON.stringify({source:'source.mp4',
    shots:[{from:0,to:3,speed:0}]}));
  const invalid = spawnSync(process.execPath, [resolve('render-ffmpeg.mjs'), 'invalid.json', 'invalid.mp4'],
    {cwd:directory,encoding:'utf8'});
  assert.notEqual(invalid.status, 0);
  assert.match(invalid.stderr, /Invalid speed/);
});

test('editing preserves black levels for full and limited range recordings', async () => {
  const directory = await mkdtemp(resolve(tmpdir(), 'clio-video-range-'));
  await mkdir(resolve(directory, 'public'));
  for (const range of ['tv', 'pc']) {
    const fixture = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error',
      '-f', 'lavfi', '-i', 'color=c=black:size=1280x870:rate=30:duration=0.3',
      '-filter_threads', '1', '-vf', `scale=in_range=tv:out_range=${range === 'pc' ? 'full' : 'tv'}`,
      '-c:v', 'libx264', '-threads', '1', '-color_range', range,
      resolve(directory, `public/${range}.mp4`)], {encoding: 'utf8'});
    assert.equal(fixture.status, 0, fixture.stderr);
    await writeFile(resolve(directory, 'edit.json'), JSON.stringify({source: `${range}.mp4`,
      shots: [{from: 0, to: 0.3}]}));
    const edited = spawnSync(process.execPath, [resolve('render-ffmpeg.mjs'), 'edit.json', 'edited.mp4'],
      {cwd: directory, encoding: 'utf8'});
    assert.equal(edited.status, 0, edited.stderr);
    const decoded = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-threads', '1',
      '-i', resolve(directory, 'edited.mp4'), '-frames:v', '1', '-filter_threads', '1',
      '-vf', 'crop=2:2:600:400', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']);
    assert.equal(decoded.status, 0, decoded.stderr.toString());
    assert.equal(decoded.stdout.length, 12);
    assert.ok([...decoded.stdout].every(channel => channel <= 2),
      `${range} source black became ${[...decoded.stdout]}`);
  }
});
