// Direct summoning plan, N12/N14/N18: type a message as a person would and send it.
//   DS_WS=ws_... DS_SID=sess_... DS_TEXT="Email bob@example.org about @explore" \
//     node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/summon_send.mjs --label ds-n18
// An `@name` typed in full that names a subagent or server becomes a summons on submit.

const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

export default async function summonSend(page, shot) {
  const { DS_WS: ws, DS_SID: sid, DS_TEXT: text } = process.env;
  if (!ws || !sid || !text) throw new Error("DS_WS, DS_SID and DS_TEXT are required");
  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "load" });
  await page.waitForTimeout(4000);
  if (await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false)) {
    await page.locator('[data-sidebar="trigger"]').first().click().catch(() => {});
    await page.waitForTimeout(600);
  }
  const editor = page.locator('[contenteditable="true"]').first();
  await editor.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Escape"); // close the @ popover the typing opened; the words stay
  await page.waitForTimeout(500);
  await shot("typed");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(8000);
  await shot("sent");
  await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 420000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await shot("answered");
}
