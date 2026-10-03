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
