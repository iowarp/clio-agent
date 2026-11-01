// Phase 4 check: the composition view names the tools an agent loads when needed.
// Opens the default agent's view from the new-conversation draft, and expands core and
// visuals, whose deferred tools appear under "Loaded when needed".
export default async function (page, shot) {
  // A new conversation's draft shows the default agent; Customize opens its view.
  await page.getByText("Customize", { exact: true }).first().click();
  await page.waitForTimeout(2500);
  await shot("composition");
  for (const plugin of ["core", "visuals"]) {
    await page.getByText(plugin, { exact: true }).first().click().catch(() => {});
    await page.waitForTimeout(800);
    const row = page.getByText(/Loaded when needed/).first();
    await row.scrollIntoViewIfNeeded().catch(() => {});
    await shot(`plugin-${plugin}`);
  }
}
