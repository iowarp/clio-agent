// Direct summoning plan, N1/N19: the composer's `/` menu and `@` popover in one conversation.
//   DS_WS=ws_... DS_SID=sess_... node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/summon_menus.mjs --label ds-menus [--width 390 --height 844 --touch]
// The sidebar is collapsed first, so the shots show the composer at its working width.

const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

async function collapseSidebar(page) {
  // The sidebar lists the workspaces; its toggle sits in the conversation header.
  if (await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "Toggle Sidebar" }).first().click().catch(() => {});
    await page.waitForTimeout(600);
  }
}

export default async function summonMenus(page, shot) {
  const ws = process.env.DS_WS;
  const sid = process.env.DS_SID;
  if (!ws || !sid) throw new Error("DS_WS and DS_SID are required");
  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "load" });
  await page.waitForTimeout(4000);
  await collapseSidebar(page);
  const editor = page.locator('[contenteditable="true"]').first();
  await editor.click();

  await page.keyboard.type("/");
  await page.waitForTimeout(1200);
  await shot("slash-menu");
  // Scroll the menu to its MCP prompt groups.
  const prompts = page.getByText(/fixture-docs prompts/).first();
  await prompts.scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(400);
  await shot("slash-menu-prompts");
  await page.keyboard.type("review");
  await page.waitForTimeout(800);
  await shot("slash-menu-ambiguous");
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");

  await page.keyboard.type("@");
  await page.waitForTimeout(2500);
  await shot("at-popover");
  await page.keyboard.type("exp");
  await page.waitForTimeout(1500);
  await shot("at-popover-filtered");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(600);
  await page.keyboard.type(" where is the spill folder written?");
  await page.waitForTimeout(400);
  await shot("chip-in-draft");
  if (process.env.DS_SEND !== "1") return;
  // N11: send it; the person's message shows the chip and the child they started.
  await page.keyboard.press("Enter");
  await page.getByText(/You started explore on this message/).first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(2000);
  await shot("summoned");
  await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 240000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await shot("answered");
}
