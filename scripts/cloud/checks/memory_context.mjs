// The memory snapshot in a session's latest context frame, with its token cost.
//   MEMORY_SESSION=sess_x node scripts/cloud/ui_check.mjs --label memory-context \
//     --steps scripts/cloud/checks/memory_context.mjs
export default async function memoryContext(page, shot) {
  const base = new URL(page.url()).origin;
  await page.goto(`${base}/workspaces/ws_default/sessions/${process.env.MEMORY_SESSION}`, {
    waitUntil: "networkidle",
  });
  // The observability panel toggle: the first of the two icons at the top right.
  await page.mouse.click(1383, 19);
  await page.waitForTimeout(800);
  await shot("panel");
  await page.mouse.click(1272, 59); // the panel's view picker
  await page.waitForTimeout(500);
  await shot("views");
  await page.getByText("Context", { exact: true }).last().click();
  await page.waitForTimeout(800);
  const snapshot = page.getByRole("button", { name: /Latest context snapshot/ });
  await snapshot.click();
  await page.getByText("Memory", { exact: true }).first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(500);
  await shot("context");
}
