// Start a new conversation with memory off from the composer.
//   node scripts/cloud/ui_check.mjs --label memory-off --steps scripts/cloud/checks/memory_off.mjs
export default async function memoryOff(page, shot) {
  const base = new URL(page.url()).origin;
  await page.goto(`${base}/workspaces/ws_default/new`, { waitUntil: "networkidle" });
  const toggle = page.getByRole("button", { name: "Memory on" });
  await toggle.waitFor({ timeout: 30000 });
  await toggle.click();
  await page.getByRole("button", { name: "Memory off for this conversation" }).waitFor();
  await shot("toggled");
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.click();
  await page.keyboard.type("What plotting preferences do you know I have? Answer in one line.");
  await page.keyboard.press("Enter");
  await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 300000 });
  await page.waitForTimeout(1500);
  await shot("answered");
  console.error(`memory-off session url: ${page.url()}`);
}
