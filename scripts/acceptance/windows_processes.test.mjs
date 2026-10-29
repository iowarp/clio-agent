import assert from 'node:assert/strict';
import test from 'node:test';
import { processTracker, redactCommand } from './windows_processes.mjs';

const app = 'D:\\acceptance\\clio-desktop.exe';
const row = (pid, parent, second, executable = '', cmd = '') => ({
  pid, parent, created: `2026-10-28T05:00:${String(second).padStart(2, '0')}Z`, executable, cmd,
});

test('redacts token arguments while retaining process and port evidence', () => {
  assert.equal(
    redactCommand('python -m clio_agent.gact --token "temporary bearer" --port 19101 --auth-token=secret'),
    'python -m clio_agent.gact --token <redacted> --port 19101 --auth-token=<redacted>',
  );
});

test('owns only the new exact executable and its descendants, including after reparenting', () => {
  const unrelated = row(1, 0, 0, app);
  const track = processTracker(app, [unrelated]);
  const desktop = row(2, 50, 1, app.toUpperCase());
  const server = row(3, 2, 2, '', 'python -m clio_agent.gact');
  const core = row(4, 3, 3, '', 'clio-core');
  assert.deepEqual(track([unrelated, core, server, desktop]).map((p) => p.pid), [4, 3, 2]);
  assert.deepEqual(track([unrelated, core, server]).map((p) => p.pid), [4, 3]);
  assert.deepEqual(track([unrelated, core]).map((p) => p.pid), [4]);
  assert.deepEqual(track([unrelated]), []);
});

test('does not acquire a reused PID or its children', () => {
  const track = processTracker(app, []);
  track([row(2, 50, 1, app)]);
  const reused = row(2, 50, 4, 'D:\\other.exe');
  assert.deepEqual(track([reused, row(3, 2, 5)]), []);
  assert.deepEqual(track([row(4, 2, 6)]), []);
});

test('does not claim an older process with a stale parent PID or a similar filename', () => {
  const track = processTracker(app, []);
  assert.deepEqual(track([
    row(2, 50, 5, app), row(3, 2, 1), row(4, 50, 6, 'D:\\other\\clio-desktop.exe'),
  ]).map((p) => p.pid), [2]);
});
