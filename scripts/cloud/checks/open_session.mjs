// Open an existing conversation (OPEN_WORKSPACE, OPEN_SESSION), open its collapsed
// activity rows and every "Run code" row, and capture it; prints the visible text lines
// that mention a repeat ("Called N times") so a grouped call can be checked.
export default async function (page, shot) {
  const ws = process.env.OPEN_WORKSPACE;
  const sid = process.env.OPEN_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(6000);
  for (const group of await page.getByRole("button", { name: /used tools|ran commands/i }).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(800);
  const rows = page.locator('[data-slot="tool-activity"]', { hasText: "Run code" });
  for (let i = 0; i < (await rows.count()); i += 1)
    await rows.nth(i).getByText("Run code", { exact: true }).first().click().catch(() => undefined);
  await page.waitForTimeout(1500);
  const repeats = page.getByText(/Called \d+ times/);
  if (await repeats.count()) await repeats.first().scrollIntoViewIfNeeded();
  await shot("session");
  const body = await page.locator("body").innerText();
  console.log(JSON.stringify({ repeats: body.split("\n").filter((l) => /Called \d+ times|calls? from this script/.test(l)) }, null, 2));
}
