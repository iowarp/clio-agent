#!/usr/bin/env node
// Drive the native desktop app (Tauri + WebKitGTK) through tauri-driver and
// capture what its window shows. Started by native.sh, which provides the
// display, the driver and the app path.
//
//   node native-drive.mjs --steps FILE [--label NAME]
//
// FILE's default export is `async (app) => {}` with:
//   app.shot(name)            screenshot of the native window
//   app.go(path)              in-app navigation (the SPA router), e.g. '/settings/usage'
//   app.waitFor(css, ms)      wait for an element
//   app.click(css)            click an element
//   app.type(css, text)       focus an element and type, as a person would
//   app.text(css)             an element's visible text
//   app.js(source, ...args)   run script in the WebView, return its value
//   app.sleep(ms)
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
};
const DRIVER = process.env.TAURI_DRIVER_URL || 'http://127.0.0.1:4444';
const APP = process.env.CLIO_DESKTOP_APP;
const RUN = process.env.VERIFY_DIR;
const label = opt('label', 'native');
const stepsFile = opt('steps', null);
const EL = 'element-6066-11e4-a52e-4f735466cecf';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wd(method, p, body, timeoutMs = 30_000) {
  const res = await fetch(`${DRIVER}${p}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`WD ${method} ${p} -> ${res.status}: ${JSON.stringify(json).slice(0, 300)}`);
  return json.value;
}

const created = await wd(
  'POST',
  '/session',
  { capabilities: { alwaysMatch: { browserName: 'wry', 'tauri:options': { application: APP } }, firstMatch: [{}] } },
  120_000,
);
const sid = created.sessionId;
const shots = [];
let n = 0;
const app = {
  sleep,
  async js(source, ...a) {
    return wd('POST', `/session/${sid}/execute/sync`, { script: source, args: a });
  },
  async shot(name) {
    const b64 = await wd('GET', `/session/${sid}/screenshot`);
    n += 1;
    const file = path.join(RUN, `${label}-${n}-${name}.png`);
    writeFileSync(file, Buffer.from(b64, 'base64'));
    shots.push(file);
  },
  async find(css) {
    try {
      return (await wd('POST', `/session/${sid}/element`, { using: 'css selector', value: css }))[EL];
    } catch {
      return null;
    }
  },
  async waitFor(css, ms = 30_000) {
    const deadline = Date.now() + ms;
    for (;;) {
      const el = await app.find(css);
      if (el) return el;
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${css}`);
      await sleep(400);
    }
  },
  async click(css) {
    const el = await app.waitFor(css);
    await wd('POST', `/session/${sid}/element/${el}/click`, {});
  },
  async type(css, value) {
    const el = await app.waitFor(css);
    await wd('POST', `/session/${sid}/element/${el}/click`, {});
    await wd('POST', `/session/${sid}/element/${el}/value`, { text: value });
  },
  async text(css) {
    const el = await app.waitFor(css);
    return wd('GET', `/session/${sid}/element/${el}/text`);
  },
  async go(route) {
    // The app is a single-page router under the custom protocol: push the route
    // and tell the router, rather than reloading the document.
    await app.js(
      'window.history.pushState({}, "", arguments[0]); window.dispatchEvent(new PopStateEvent("popstate"));',
      route,
    );
    await sleep(2500);
  },
};

const report = { label, shots, errors: [] };
try {
  await app.waitFor('section[aria-label="Session workspace"], nav[aria-label="Workspace navigation"], form', 90_000).catch(() => {});
  await sleep(3000);
  await app.shot('launched');
  report.location = await app.js('return window.location.href;');
  if (stepsFile) {
    const steps = (await import(pathToFileURL(path.resolve(stepsFile)).href)).default;
    await steps(app);
  }
} catch (error) {
  report.errors.push(String(error && error.stack ? error.stack : error));
  await app.shot('failure').catch(() => {});
} finally {
  await wd('DELETE', `/session/${sid}`).catch(() => {});
}
mkdirSync(RUN, { recursive: true });
writeFileSync(path.join(RUN, `${label}-report.json`), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
process.exit(report.errors.length ? 1 : 0);
