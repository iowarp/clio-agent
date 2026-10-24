#!/usr/bin/env node
// Drive the gact-tui web UI in headless Chromium and capture what it shows.
//
// Run from the repository root after `bash scripts/cloud/stack.sh start`:
//
//   node scripts/cloud/ui_check.mjs                         # connect, screenshot the workspace
//   node scripts/cloud/ui_check.mjs --send "List the plugins you have"
//   node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/runs_view.mjs
//   node scripts/cloud/ui_check.mjs --label plugins --width 390 --height 844   # phone width
//
// --steps names a module whose default export is `async (page, shot) => {}`; call
// `await shot("name")` to capture a screenshot at any point. Screenshots land in
// .cloud-run/shots/<label>-<n>-<name>.png, and the script prints their paths, the
// browser console errors and any failed requests so the agent can read the images
// with its file reader and check the run for errors.

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

function chromiumPath() {
  const base = "/opt/pw-browsers";
  if (!existsSync(base)) return undefined;
  const dirs = readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort();
  for (const d of dirs.reverse()) {
    const exe = path.join(base, d, "chrome-linux/chrome");
    if (existsSync(exe)) return exe;
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
  const name = page.getByPlaceholder("For example, Homelab");
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  // Wait for whichever renders: the connect form (first run) or a restored workspace.
  const first = await Promise.race([
    name.waitFor({ timeout: 45000 }).then(() => "form"),
    composer.waitFor({ timeout: 45000 }).then(() => "workspace"),
  ]).catch(() => "neither");
  if (first === "neither") throw new Error("the UI showed neither the connect form nor a workspace");
  if (first === "form") {
    await name.fill("cloud");
    // The form defaults to http://127.0.0.1:8787; only retype it for another agent.
    if (agentUrl !== "http://127.0.0.1:8787") {
      await page.getByText("Connection address").locator("..").getByRole("textbox").fill(agentUrl);
    }
    await page.getByRole("button", { name: /Connect/ }).click();
    await name.waitFor({ state: "hidden", timeout: 30000 }).catch(() => {
      throw new Error(`could not connect to ${agentUrl}; is the stack running?`);
    });
  }
  await page.waitForTimeout(3000);
  await shot("connected");

  if (send) {
    await composer.click();
    await page.keyboard.type(send);
    await page.keyboard.press("Enter");
    // A finished turn shows its "Done" status row; give up after --settle ms.
    await page.getByText("Done", { exact: true }).last().waitFor({ timeout: settleMs }).catch(() => {});
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
  await browser.close();
}

console.log(JSON.stringify({ shots, consoleErrors, failedRequests }, null, 2));
