// CN5: the connector setup card's line under the call it held, read from an existing
// conversation (SETUP_SESSION=/workspaces/<ws>/sessions/<sid>). Expands the turn's
// activity and prints the line's text.
export default async function (page, shot) {
  await page.goto(new URL(process.env.SETUP_SESSION, page.url()).toString());
  await page.waitForTimeout(6000);
  for (const group of await page.getByRole("button", { name: /Called \d+ tools?/i }).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(2000);
  const lines = await page.locator('[data-slot="connector-setup-activity"]').allInnerTexts();
  await shot("setup-line");
  console.error(JSON.stringify({ lines }));
}
