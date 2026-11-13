#!/usr/bin/env node
// Drive the gact-tui web UI in headless Chromium and capture what it shows.
//
// Run from the repository root after `bash scripts/cloud/stack.sh start`:
//
//   node scripts/cloud/ui_check.mjs                         # connect, screenshot the workspace
//   node scripts/cloud/ui_check.mjs --send "List the plugins you have"
//   node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/runs_view.mjs
//   node scripts/cloud/ui_check.mjs --label plugins --width 390 --height 844   # phone width
//   node scripts/cloud/ui_check.mjs --width 390 --height 844 --touch --steps FILE  # touch device
//
// --video records the whole session to .cloud-run/shots/<label>.webm (live updates,
// not just stills).
//
// --steps names a module whose default export is `async (page, shot) => {}`; call
// `await shot("name")` to capture a screenshot at any point. Screenshots land in
// .cloud-run/shots/<label>-<n>-<name>.png, and the script prints their paths, the
// browser console errors and any failed requests so the agent can read the images
// with its file reader and check the run for errors. --touch gives the page a touch
// screen and mobile emulation (`hasTouch`, `isMobile`) so steps can use `locator.tap()`.
//
// The browser starts with --agent already remembered as the UI's service, so the UI never
// tries the product default address first, and with software WebGL (SwiftShader) for
// views such as Mol* and trame; checks/apps/webgl.mjs reports what it can draw.

import { createRequire } from "node:module";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const web = path.join(root, "external/gact-tui/web");
const require = createRequire(path.join(web, "package.json"));
const { chromium } = require("@playwright/test");

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : fallback;
};
const uiUrl = opt("ui", `http://127.0.0.1:${process.env.UI_PORT || 5173}/`);
const agentUrl = opt("agent", `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`);
const label = opt("label", "ui");
const width = Number(opt("width", 1440));
const height = Number(opt("height", 900));
const send = opt("send", null);
const stepsFile = opt("steps", null);
const settleMs = Number(opt("settle", 60000));
// --video: record the whole session as <label>.webm in the shots folder (live updates).
const video = args.includes("--video");
// --touch: a touch device (hasTouch, isMobile), so steps can tap and drag with real touches.
const touch = args.includes("--touch");

function chromiumPath() {
  const base = "/opt/pw-browsers";
  if (!existsSync(base)) return undefined;
  // The image links its browser here; older images only have versioned dirs.
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

// Mol* and trame's local view draw with WebGL. Headless Chromium 153 here falls back to
// SwiftShader (WebGL 1 and 2) on its own; Chrome is deprecating that automatic fallback,
// so the software renderer is asked for explicitly (apps ledger, grounds).
const launchArgs = ["--enable-unsafe-swiftshader"];
let browser;
try {
  browser = await chromium.launch({ args: launchArgs });
} catch {
  browser = await chromium.launch({ args: launchArgs, executablePath: chromiumPath() });
}
const context = await browser.newContext({
  viewport: { width, height },
  ...(touch ? { hasTouch: true, isMobile: true } : {}),
  ...(video ? { recordVideo: { dir: path.join(root, ".cloud-run/video-tmp"), size: { width, height } } } : {}),
});
// Start the UI already pointed at this stack's agent: seed the remembered service before
// the first load, so the UI never tries the product default address (127.0.0.1:17800)
// first. Only the UI's own origin is touched, and a service a step file saved is kept.
await context.addInitScript(
  ([origin, endpoint]) => {
    if (window.location.origin !== origin) return;
    try {
      if (!window.localStorage.getItem("clio.recent-connections")) {
        window.localStorage.setItem("clio.recent-connections", JSON.stringify([{ endpoint, label: "cloud" }]));
      }
    } catch {
      // Storage refused (opaque origin): the connect form below still handles it.
    }
  },
  [new URL(uiUrl).origin, agentUrl],
);
const page = await context.newPage();
const consoleErrors = [];
const failedRequests = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text().slice(0, 300)));
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));
// Polling requests cancelled by navigation or browser shutdown report ERR_ABORTED; they are not failures.
page.on("requestfailed", (r) => {
  const reason = r.failure()?.errorText ?? "";
  if (!reason.includes("ERR_ABORTED")) failedRequests.push(`${r.method()} ${r.url()} ${reason}`);
});
page.on("response", (r) => {
  if (r.status() >= 400 && r.url().startsWith(agentUrl)) {
    failedRequests.push(`${r.request().method()} ${r.url()} HTTP ${r.status()}`);
  }
});

let n = 0;
const shots = [];
const shot = async (name) => {
  n += 1;
  const file = path.join(shotsDir, `${label}-${n}-${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  shots.push(file);
  return file;
};

try {
  await page.goto(uiUrl, { waitUntil: "networkidle", timeout: 60000 });

  // First run shows the "Add an agent service" form; later runs restore the saved service.
  // The form's address starts empty, so it is always filled with --agent.
  const name = page.locator("#endpoint");
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  // Wait for whichever renders: the connect form (first run) or a restored workspace.
  const first = await Promise.race([
    name.waitFor({ timeout: 45000 }).then(() => "form"),
    composer.waitFor({ timeout: 45000 }).then(() => "workspace"),
  ]).catch(() => "neither");
  if (first === "neither") throw new Error("the UI showed neither the connect form nor a workspace");
  if (first === "form") {
    await name.fill(agentUrl);
    await page.locator("#service-name").fill("cloud").catch(() => {});
    await page.getByRole("button", { name: /Connect/ }).click();
    await name.waitFor({ state: "hidden", timeout: 30000 }).catch(() => {
      throw new Error(`could not connect to ${agentUrl}; is the stack running?`);
    });
  }
  await page.waitForTimeout(3000);
  await shot("connected");

  if (send) {
    // The UI posts the message to /v1/sessions/<id>/messages; the turn has finished when
    // the agent reports that session idle again. Give up after --settle ms.
    const posted = page
      .waitForRequest((r) => r.method() === "POST" && /\/v1\/sessions\/[^/]+\/messages$/.test(r.url()), {
        timeout: 30000,
      })
      .catch(() => null);
    await composer.click();
    await page.keyboard.type(send);
    await page.keyboard.press("Enter");
    const request = await posted;
    const sid = request ? new URL(request.url()).pathname.split("/")[3] : "";
    const deadline = Date.now() + settleMs;
    while (sid && Date.now() < deadline) {
      await page.waitForTimeout(1000);
      const state = await fetch(`${agentUrl}/v1/sessions/${sid}`).then((r) => r.json()).catch(() => ({}));
      if (state.status === "idle") break;
    }
    if (!sid) consoleErrors.push("ui_check: the UI posted no message to the agent");
    await page.waitForTimeout(1000);
    await shot("after-send");
  }

  if (stepsFile) {
    const mod = await import(pathToFileURL(path.resolve(stepsFile)).href);
    await mod.default(page, shot);
  }
} catch (error) {
  consoleErrors.push(`ui_check: ${error.message.split("\n")[0]}`);
  await shot("failure").catch(() => {});
  process.exitCode = 1;
} finally {
  const recording = video ? page.video() : null;
  await context.close(); // flushes the recording
  if (recording) {
    const file = path.join(shotsDir, `${label}.webm`);
    await recording.saveAs(file);
    await recording.delete();
    shots.push(file);
  }
  await browser.close();
}

console.log(JSON.stringify({ shots, consoleErrors, failedRequests }, null, 2));
