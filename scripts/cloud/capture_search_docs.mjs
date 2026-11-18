#!/usr/bin/env node
// Captures the website images for web search, literature search and deep research
// (site/src/assets/captures/search/) from real sessions on a running stack.
//
//   bash scripts/cloud/stack.sh start        # with a fresh CLIO_AGENT_HOME, no keys
//   node scripts/cloud/capture_search_docs.mjs [--out DIR] [--skip-deep]
//
// The requests are plain questions. The script answers only CLIO's search-source
// approvals and allows the four search tools for each session, as the person would;
// any other question is printed and left unanswered. Screenshots are taken at two
// device pixels per CSS pixel. A deep research run takes 15 to 40 minutes.
import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
// The UI checkout's Playwright when it is installed, else the machine's.
const loadChromium = () => {
  try {
    return createRequire(path.join(root, "external/gact-tui/web/package.json"))("@playwright/test")
      .chromium;
  } catch {
    return createRequire(import.meta.url)("playwright").chromium;
  }
};
const chromium = loadChromium();

const { values: args } = parseArgs({
  options: {
    out: { type: "string", default: "site/src/assets/captures/search" },
    ui: { type: "string", default: "http://127.0.0.1:5173/" },
    agent: { type: "string", default: "http://127.0.0.1:8787" },
    "skip-deep": { type: "boolean", default: false },
  },
});
const OUT = path.resolve(args.out);
const AGENT = args.agent;
const SEARCH_TOOLS = ["web_search", "web_fetch", "literature_search", "literature_lookup"];
const CHAT =
  "What changed in the HDF5 2.0 release compared with 1.14? Also find three recent papers " +
  "on lossy compression of scientific data stored in HDF5, with DOIs.";
const REVIEW =
  "Write a short literature review of lossless compression for scientific floating-point " +
  "data since 2020: which methods are used, which of them are available as HDF5 filters, " +
  "and what compression ratios and speeds papers report.";
mkdirSync(OUT, { recursive: true });

const api = async (p, body) => {
  const init = body
    ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }
    : {};
  return (await fetch(AGENT + p, init)).json();
};

async function openUi({ width = 1440, height = 1000, mobile = false } = {}) {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 2,
    isMobile: mobile,
    hasTouch: mobile,
  });
  // A returning user: no product tour over the captures.
  await context.addInitScript(() => {
    try {
      localStorage.setItem("clio.onboarding.v1", JSON.stringify({ install: "existing", status: "skipped" }));
    } catch {}
  });
  const page = await context.newPage();
  await page.goto(args.ui, { waitUntil: "networkidle", timeout: 60000 });
  const endpoint = page.locator("#endpoint");
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  const first = await Promise.race([
    endpoint.waitFor({ timeout: 45000 }).then(() => "form"),
    composer.waitFor({ timeout: 45000 }).then(() => "workspace"),
  ]);
  if (first === "form") {
    await endpoint.fill(AGENT);
    await page.locator("#service-name").fill("Lab workstation").catch(() => {});
    await page.getByRole("button", { name: /Connect/ }).click();
    await endpoint.waitFor({ state: "hidden", timeout: 30000 });
  }
  await page.waitForTimeout(2500);
  return { browser, page, composer };
}

const shot = async (page, name) => {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(file);
};

/** Answer search approvals and allow search tools; report anything else once. */
async function answerSearch(sid, seen, onFirst) {
  const rows = (await api(`/v1/sessions/${sid}/interactions?include_children=true`)).interactions || [];
  for (const row of rows.filter((r) => r.status === "pending")) {
    const tool = row.payload?.tool_call?.tool_name || "";
    const values = (row.payload?.options || []).map((o) => o.value);
    let body = null;
    if (row.kind === "permission" && SEARCH_TOOLS.includes(tool)) body = { action: "allow_session" };
    else if (values.includes("approve")) body = { action: "answer", answer: "approve", selected_options: ["approve"], metadata: { choice: "approve" } };
    else if (values.includes("switch")) body = { action: "answer", answer: "switch", selected_options: ["switch"], metadata: { choice: "switch" } };
    if (!body) {
      if (!seen.has(row.id)) console.log("left for the person:", row.kind, row.title || row.prompt);
      seen.add(row.id);
      continue;
    }
    await onFirst?.(row);
    await api(`/v1/sessions/${sid}/interactions/${row.id}/respond`, body);
  }
}

async function send(page, composer, text) {
  const posted = page.waitForResponse(
    (r) => /\/v1\/sessions\/[^/]+\/messages/.test(r.url()) && r.request().method() === "POST",
    { timeout: 90000 },
  );
  await composer.click();
  await composer.fill(text);
  await page.waitForTimeout(500);
  await composer.press("Enter");
  return (await posted).url().match(/sessions\/([^/]+)\/messages/)[1];
}

async function settle(page, sid, minutes, onFirst, progress) {
  const seen = new Set();
  const t0 = Date.now();
  while (Date.now() - t0 < minutes * 60000) {
    await page.waitForTimeout(4000);
    await answerSearch(sid, seen, onFirst);
    await progress?.((Date.now() - t0) / 60000);
    const status = (await api(`/v1/sessions/${sid}`)).status;
    if (Date.now() - t0 > 30000 && ["idle", "completed", "error"].includes(status)) return status;
  }
  return "timeout";
}

// 1. Settings, desktop and phone.
for (const [label, size] of [["", {}], ["-phone", { width: 390, height: 844, mobile: true }]]) {
  const { browser, page } = await openUi({ height: 900, ...size });
  await page.goto(new URL("/settings/web-search", page.url()).href, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  await shot(page, `settings-web${label}`);
  if (!label) {
    await page.getByRole("radio", { name: "Literature" }).click();
    await page.waitForTimeout(1500);
    await shot(page, "settings-literature");
  }
  await browser.close();
}

// 2. A plain chat request: the permission request, the first-use question, results.
{
  const { browser, page, composer } = await openUi();
  const sid = await send(page, composer, CHAT);
  const taken = new Set();
  const status = await settle(page, sid, 20, async (row) => {
    const name = row.kind === "permission" ? "chat-permission" : "chat-approval";
    if (taken.has(name)) return;
    taken.add(name);
    await page.waitForTimeout(2500);
    await shot(page, name);
  });
  console.log("chat", status);
  await page.mouse.move(900, 400);
  for (let i = 0; i < 30; i += 1) await page.mouse.wheel(0, -2000);
  await page.getByText(/^Called \d+ tools/).first().click();
  await page.waitForTimeout(1500);
  await page.getByText("Search literature", { exact: true }).first().click();
  await page.waitForTimeout(2500);
  await shot(page, "chat-literature-results");
  const web = page.getByText("Search web", { exact: true }).first();
  await web.scrollIntoViewIfNeeded();
  await web.click();
  await page.waitForTimeout(2500);
  await shot(page, "chat-web-results");
  await browser.close();
}

// 3. A plain literature-review request in Deep research mode.
if (!args["skip-deep"]) {
  const { browser, page, composer } = await openUi();
  await page.getByRole("button", { name: /Execute/ }).first().click();
  await page.waitForTimeout(1200);
  await shot(page, "deep-mode-menu");
  await page.getByText("Deep research", { exact: true }).first().click();
  const sid = await send(page, composer, REVIEW);
  writeFileSync(path.join(OUT, ".deep-session"), sid);
  let running = false;
  const status = await settle(page, sid, 75, null, async (minutes) => {
    if (!running && minutes > 6) {
      running = true;
      await shot(page, "deep-running");
    }
  });
  console.log("deep research", status);
  await page.waitForTimeout(5000);
  await shot(page, "deep-delivered");
  await browser.close();
}
