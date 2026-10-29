// Export a conversation through the conversation menu, as a person does, and save
// the download. Every live scenario's evidence includes this export.
//
//   EXPORT_SID=sess_... EXPORT_WS=ws_... EXPORT_OUT=.cloud-run/f1 EXPORT_MODE=html \
//     node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/export_session.mjs --label f1-export
//
// EXPORT_MODE is `html` (transcript only) or `zip` (transcript plus session artifacts).
// The saved file's path is printed as `export: <path>`.

import { mkdirSync } from "node:fs";
import path from "node:path";

const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

export default async (page, shot) => {
  const sid = process.env.EXPORT_SID;
  const ws = process.env.EXPORT_WS || "ws_default";
  const out = path.resolve(process.env.EXPORT_OUT || ".cloud-run/exports");
  const mode = process.env.EXPORT_MODE || "html";
  if (!sid) throw new Error("EXPORT_SID is required");
  mkdirSync(out, { recursive: true });

  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(4000);
  const triggers = page.getByRole("button", { name: /^Actions for / });
  await triggers.nth((await triggers.count()) - 1).click();
  await page.getByRole("menuitem", { name: /Export session/ }).hover();
  if (mode === "zip") {
    await page.getByRole("menuitemcheckbox", { name: /Include session artifacts/ }).click();
  }
  await shot(`export-menu-${mode}`);
  const download = page.waitForEvent("download", { timeout: 180000 });
  await page.getByRole("menuitem", { name: mode === "zip" ? /Download ZIP/ : /Download HTML/ }).click();
  const file = await download;
  const target = path.join(out, `${sid}.${mode === "zip" ? "zip" : "html"}`);
  await file.saveAs(target);
  console.log(`export: ${target}`);
};
