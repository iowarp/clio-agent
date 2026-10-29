#!/usr/bin/env node
// Open a session export offline in headless Chromium and capture what it shows.
//
//   node scripts/cloud/export_check.mjs FILE.html [--label NAME] [--steps FILE] [--width W --height H]
//
// Every network request except the file itself is blocked, so the capture shows
// what a reader without CLIO sees. Without --steps the script screenshots each
// top-level tab of the archive. --steps names a module whose default export is
// `async (page, shot) => {}`. Screenshots land in .cloud-run/shots/<label>-<n>-<name>.png;
// the script prints them with console errors and blocked requests as JSON.

import { createRequire } from "node:module";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const require = createRequire(path.join(root, "external/gact-tui/web/package.json"));
const { chromium } = require("@playwright/test");

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
};
const file = path.resolve(args[0] ?? "");
if (!args[0] || !existsSync(file)) {
  console.error("usage: export_check.mjs FILE.html [--label NAME] [--steps FILE]");
  process.exit(2);
}
const label = opt("label", path.basename(file, path.extname(file)));
const width = Number(opt("width", 1440));
const height = Number(opt("height", 900));
const stepsFile = opt("steps", null);

function chromiumPath() {
  const base = "/opt/pw-browsers";
  if (!existsSync(base)) return undefined;
  if (existsSync(path.join(base, "chromium"))) return path.join(base, "chromium");
  const dirs = readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort();
  for (const d of dirs.reverse()) {
    for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
      const exe = path.join(base, d, sub);
      if (existsSync(exe)) return exe;
    }
  }
  return undefined;
}

const shotsDir = path.join(root, ".cloud-run/shots");
mkdirSync(shotsDir, { recursive: true });
let browser;
try {
  browser = await chromium.launch();
} catch {
  browser = await chromium.launch({ executablePath: chromiumPath() });
}
const page = await browser.newPage({ viewport: { width, height } });
const consoleErrors = [];
const blocked = [];
const self = pathToFileURL(file).href;
await page.route("**/*", (route) => {
  const url = route.request().url();
  if (url === self || url.startsWith("data:") || url.startsWith("blob:")) return route.continue();
  if (url.startsWith("file://") && url.startsWith(pathToFileURL(path.dirname(file)).href)) {
    return route.continue();
  }
  blocked.push(url);
  return route.abort();
});
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text().slice(0, 300)));
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));

let n = 0;
const shots = [];
const shot = async (name, options = {}) => {
  n += 1;
  const out = path.join(shotsDir, `${label}-${n}-${name}.png`);
  await page.screenshot({ path: out, fullPage: false, ...options });
  shots.push(out);
  return out;
};

try {
  await page.goto(self, { waitUntil: "load", timeout: 120000 });
  await page.waitForTimeout(5000);
  if (stepsFile) {
    const mod = await import(pathToFileURL(path.resolve(stepsFile)).href);
    await mod.default(page, shot);
  } else {
    await shot("open");
    const tabs = page.getByRole("tab");
    const count = await tabs.count();
    for (let i = 0; i < count; i += 1) {
      const tab = tabs.nth(i);
      const name = ((await tab.textContent()) || `tab${i}`).trim().replace(/[^A-Za-z0-9]+/g, "-");
      await tab.click();
      await page.waitForTimeout(1500);
      await shot(name.toLowerCase());
    }
  }
} catch (error) {
  consoleErrors.push(`export_check: ${String(error).slice(0, 300)}`);
  await shot("failure").catch(() => undefined);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ shots, consoleErrors, blocked: [...new Set(blocked)] }, null, 2));
