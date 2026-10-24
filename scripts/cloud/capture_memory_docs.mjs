#!/usr/bin/env node
// Light-mode, 2x captures of long-term memory for the docs site, from the running stack.
//
//   node scripts/cloud/capture_memory_docs.mjs --out .cloud-run/docs-captures \
//     --heatmap sess_x --colormap sess_y --notice sess_z
//
// Every capture is the real product on real sessions of the live verification run
// (docs/design/sdk/memory-ledger.md); nothing is painted or composited.
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import path from "node:path";

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const require = createRequire(path.join(root, "external/gact-tui/web/package.json"));
const { chromium } = require("@playwright/test");
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const out = path.resolve(opt("out", ".cloud-run/docs-captures"));
const ui = opt("ui", "http://127.0.0.1:5173");
mkdirSync(out, { recursive: true });

function chromiumPath() {
  const base = "/opt/pw-browsers";
  if (!existsSync(base)) return undefined;
  const dirs = readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse();
  return dirs.map((d) => path.join(base, d, "chrome-linux/chrome")).find((exe) => existsSync(exe));
}
const browser = await chromium
  .launch()
  .catch(() => chromium.launch({ executablePath: chromiumPath() }));
const context = await browser.newContext({
  viewport: { width: 1200, height: 820 },
  deviceScaleFactor: 2,
  colorScheme: "light",
});
// The product defaults to dark (next-themes, storage key "theme"); the docs use light.
await context.addInitScript(() => window.localStorage.setItem("theme", "light"));
const page = await context.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
const shot = async (name, target) => {
  const file = path.join(out, `docs-memory-${name}.png`);
  await (target ?? page).screenshot({ path: file });
  console.log(file);
};

try {
  await page.goto(ui, { waitUntil: "networkidle" });
  const name = page.getByPlaceholder("For example, Homelab");
  if (await name.isVisible({ timeout: 15000 }).catch(() => false)) {
    await name.fill("cloud");
    await page.getByRole("button", { name: /Connect/ }).click();
    await name.waitFor({ state: "hidden", timeout: 30000 });
  }
  await page.waitForTimeout(2000);

  // 1. What is remembered, by scope and category.
  await page.goto(`${ui}/settings/memory`, { waitUntil: "networkidle" });
  const remembered = page.getByRole("heading", { name: "What is remembered" });
  await remembered.waitFor({ timeout: 30000 });
  await remembered.evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.mouse.wheel(0, -24);
  await page.waitForTimeout(600);
  await shot("documents");

  // 2. The review inbox.
  const inbox = page.getByRole("list", { name: "Proposed changes" });
  if (await inbox.isVisible().catch(() => false)) {
    await inbox.scrollIntoViewIfNeeded();
    await page.waitForTimeout(400);
    await shot("inbox", inbox.getByRole("listitem").first());
  }

  // 3. A document with its sources, and 4. its history.
  await page.goto(`${ui}/settings/memory?doc=${encodeURIComponent("user/preferences/preferences")}`, {
    waitUntil: "networkidle",
  });
  const dialog = page.getByRole("dialog");
  await dialog.waitFor({ timeout: 20000 });
  await page.waitForTimeout(700);
  await shot("document", dialog);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Actions for preferences" }).click();
  await page.getByRole("menuitem", { name: "History" }).click();
  await page.getByRole("dialog").waitFor({ timeout: 20000 });
  await page.waitForTimeout(700);
  await shot("history", page.getByRole("dialog"));
  await page.keyboard.press("Escape");

  // 5. An answer that used remembered preferences, and 6. its memory popover.
  const session = async (sid) => {
    await page.goto(`${ui}/workspaces/ws_default/sessions/${sid}`, { waitUntil: "networkidle" });
    await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 30000 });
    await page.waitForTimeout(800);
  };
  await session(opt("heatmap"));
  await shot("answer");
  await session(opt("colormap"));
  await page.getByRole("button", { name: /^Used \d+ memory document/ }).last().click();
  await page.waitForTimeout(700);
  await shot("popover");
  await page.keyboard.press("Escape");

  // 7. A memory write in the conversation.
  await session(opt("notice"));
  await page.getByRole("button", { name: /updated memory/i }).last().click();
  await page.waitForTimeout(500);
  await page.getByText("Memory updated").last().click();
  await page.waitForTimeout(600);
  await shot("notice");

  // 8. Memory off for a new conversation.
  await page.goto(`${ui}/workspaces/ws_default/new`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Memory on" }).click();
  await page.waitForTimeout(400);
  await shot("off", page.getByRole("group", { name: "Message behavior" }));
} finally {
  await browser.close();
}
if (errors.length) {
  console.error(JSON.stringify(errors, null, 2));
  process.exitCode = 1;
}
