// Open an existing conversation (OPEN_WORKSPACE, OPEN_SESSION), open its collapsed
// activity rows and every "Run code" row, and capture it; prints the visible text lines
// that mention a repeat ("Called N times") so a grouped call can be checked. With
// OPEN_LINK=1 it then follows the last "Show the task in Work" link and captures it;
// with OPEN_COLLAPSED=1 it leaves the activity groups as a person first sees them, and
// OPEN_FOCUS (a CSS selector) scrolls that element into view for the capture.
// OPEN_ROW names the action of the rows to open (default "Run code").
export default async function (page, shot) {
  const ws = process.env.OPEN_WORKSPACE;
  const sid = process.env.OPEN_SESSION;
  const action = process.env.OPEN_ROW || "Run code";
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(6000);
  if (process.env.OPEN_COLLAPSED !== "1")
    for (const group of await page.getByRole("button", { name: /used tools|ran commands/i }).all())
      await group.click().catch(() => undefined);
  await page.waitForTimeout(800);
  const rows = page.locator('[data-slot="tool-activity"]', { hasText: action });
  for (let i = 0; i < (await rows.count()); i += 1)
    await rows.nth(i).getByText(action, { exact: true }).first().click().catch(() => undefined);
  await page.waitForTimeout(1500);
  const repeats = page.getByText(/Called \d+ times/);
  if (await repeats.count()) await repeats.first().scrollIntoViewIfNeeded();
  if (process.env.OPEN_FOCUS) {
    // Scroll a chosen element (a CSS selector) into view before the capture.
    const focus = page.locator(process.env.OPEN_FOCUS).first();
    if (await focus.count()) await focus.scrollIntoViewIfNeeded();
    console.log(JSON.stringify({ focus: process.env.OPEN_FOCUS, found: await focus.count() }));
  }
  await shot("session");
  const body = await page.locator("body").innerText();
  const links = page.getByText("Show the task in Work", { exact: true });
  if (process.env.OPEN_LINK === "1" && (await links.count())) {
    // The last launch's link: a finished task must still show its row (Completed work).
    await links.last().click();
    await page.waitForTimeout(2000);
    await shot("task-link");
  }
  console.log(JSON.stringify({ repeats: body.split("\n").filter((l) => /Called \d+ times|calls? from this script/.test(l)) }, null, 2));
}
