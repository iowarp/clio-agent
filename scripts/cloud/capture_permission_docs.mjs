#!/usr/bin/env node
// Light-mode, 2x captures of the sandbox folder flow for the permissions page (#1905).
//
//   node scripts/cloud/capture_permission_docs.mjs --out .cloud-run/docs-captures \
//     --root /home/user/Projects/bike-counts --outside /home/user/Reports
//
// The workspace folder must be inside the server's CLIO_ALLOWED_ROOTS and hold counts.csv;
// the outside folder must not be. A real model run copies the file with a tool-directed
// prompt (no access_paths), so the sandbox blocks it; the captures are the folder access
// card it raises, the notice on the message, the Allow dialog, and, after allowing, the
// folders the agent may write in Evidence. Nothing is painted or composited.
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
const agent = opt("agent", "http://127.0.0.1:8787");
const workspaceRoot = opt("root");
const outside = opt("outside");
mkdirSync(out, { recursive: true });

async function api(method, route, body) {
  const response = await fetch(agent + route, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

function chromiumPath() {
  const base = "/opt/pw-browsers";
  if (!existsSync(base)) return undefined;
  // The cloud image links its browser here (as scripts/cloud/ui_check.mjs finds it).
  if (existsSync(path.join(base, "chromium"))) return path.join(base, "chromium");
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
  const file = path.join(out, `docs-permissions-${name}.png`);
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
  const created = await api("POST", "/v1/workspaces", {
    name: path.basename(workspaceRoot),
    root_path: workspaceRoot,
  });
  // A rerun finds the workspace already made for this folder; use it.
  const workspace = created.id ? created : { id: created.error?.details?.workspace_id };
  if (!workspace.id) throw new Error(`no workspace: ${JSON.stringify(created)}`);
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(`${ui}/workspaces/${workspace.id}/sessions/${session.id}`, {
    waitUntil: "networkidle",
  });
  await page.waitForTimeout(3000);
  const composer = page.locator('[contenteditable="true"]').last();
  await composer.waitFor({ timeout: 90000 });
  await composer.click();
  await page.keyboard.type(
    `Run exactly this shell command with shell_bash and no access_paths: cp counts.csv ${outside}/counts.csv, then tell me what happened in two sentences.`,
  );
  await page.keyboard.press("Enter");
  let quiet = 0;
  for (let i = 0; i < 120 && quiet < 3; i += 1) {
    // Approve the command itself as a person would; the folder card is what is captured.
    const pending = await api("GET", `/v1/permissions?session_id=${session.id}&status=pending`);
    for (const row of pending.permissions || [])
      if (row.kind !== "folder_access")
        await api("POST", `/v1/permissions/${row.id}`, { action: "allow_session" });
    const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
    quiet = ["idle", "completed"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(2000);

  // 1. The folder access card the block raised.
  const card = page.getByText("Allow access to another folder?").first();
  await card.waitFor({ timeout: 30000 });
  const cardPanel = page.locator('section, div').filter({ has: card }).filter({ hasText: "Deny" }).last();
  await cardPanel.scrollIntoViewIfNeeded();
  await shot("folder-card", cardPanel);

  // 2. The notice on the message.
  const notice = page.locator('[data-slot="turn-sandbox-notices"]').first();
  await notice.scrollIntoViewIfNeeded();
  await shot("blocked-notice", notice);

  // 3. The dialog its Allow opens.
  await notice.getByRole("button", { name: /^Allow / }).click();
  const dialog = page.getByRole("alertdialog");
  await dialog.waitFor();
  await page.waitForTimeout(800); // past the open animation
  await shot("allow-dialog", dialog);
  await page.getByRole("button", { name: "Allow folder" }).click();
  for (let i = 0; i < 30; i += 1) {
    if (await notice.getByText(/^Allowed /).count()) break;
    await page.waitForTimeout(1000);
  }

  // 4. The folders the agent may write, in Observability, Evidence.
  const openCanvas = page.getByRole("button", { name: "Open workspace canvas" });
  if (await openCanvas.count()) await openCanvas.first().click();
  await page.waitForTimeout(1000);
  await page.getByTitle("Open a canvas tab").click();
  await page.getByRole("menuitem", { name: /Observability/ }).first().click();
  const folders = page.locator('section[aria-label="Folders the agent may write"]');
  await folders.waitFor({ timeout: 30000 });
  await shot("allowed-folders", folders);
  console.log(JSON.stringify({ session: session.id, workspace: workspace.id, errors }));
} catch (error) {
  await page.screenshot({ path: path.join(out, "docs-permissions-failure.png") }).catch(() => {});
  throw error;
} finally {
  await browser.close();
}
