// Tools page check (TD54): a plugin's Tools tab with read-only and write groups, the
// approval-mode outcomes, per-tool controls and, at phone width, the detail screen.
//   node scripts/cloud/ui_check.mjs --label tools --steps scripts/cloud/checks/tool_settings.mjs
// TOOLS_PLUGIN names the installed plugin to open (default: core).
export default async function toolSettings(page, shot) {
  const base = new URL(page.url()).origin;
  const plugin = process.env.TOOLS_PLUGIN || "core";
  await page.goto(`${base}/settings/plugins`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await page.waitForTimeout(800);
  const explore = page.getByRole("button", { name: `Explore ${plugin}` }).first();
  if (!(await explore.isVisible().catch(() => false))) {
    await page.getByRole("button", { name: new RegExp(`^${plugin}\\b`) }).first().click();
  }
  await explore.click();
  await page.getByRole("dialog").waitFor({ timeout: 15000 });
  await page.getByRole("tab", { name: "Tools" }).click();
  await page.getByText(/Read-only tools|Write and delete tools/).first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(800);
  await shot("tools");
  const wide = (page.viewportSize()?.width ?? 0) >= 640;
  if (wide) {
    await page.getByRole("combobox", { name: "Show what happens in" }).click();
    await page.getByRole("option", { name: "Workspace edits" }).click();
    await page.waitForTimeout(500);
    await shot("auto-edits");
    await page.getByText("Write and delete tools").first().scrollIntoViewIfNeeded();
    await shot("write-group");
    await page.getByRole("combobox", { name: /^Permission: / }).nth(2).click();
    await page.waitForTimeout(300);
    await shot("permission-menu");
    await page.keyboard.press("Escape");
  } else {
    await page.getByRole("button", { name: /^write_todos: / }).click();
    await page.getByRole("radiogroup", { name: "Permission" }).waitFor({ timeout: 10000 });
    await shot("detail");
    await page.getByRole("radiogroup", { name: "I/O" }).scrollIntoViewIfNeeded();
    await shot("detail-io");
    await page.getByRole("button", { name: "Back to tools" }).click();
    await page.waitForTimeout(300);
    await shot("back");
  }
}
