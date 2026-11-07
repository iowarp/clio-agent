// W9 check: the person clicks in the live view while the agent's script waits.
// CHECK_ASK is sent; questions are answered as they come; once the page appears in the
// Work rail (the turn still running), it is opened and the button inside its frame is
// clicked on the live frame (its position measured on a copy of the fixture page).
const ALLOW =
  /^(Allow once|Allow for session|Allow for workspace|Allow for the rest of this script)$/;
const FRAMES = process.env.CHECK_FRAMES_URL || "http://a.fixture.localhost:8790/frames";

async function buttonBox(page) {
  const probe = await page.context().browser().newPage();
  await probe.setViewportSize({ width: 1280, height: 800 });
  await probe.goto(FRAMES);
  const box = await probe.frameLocator("iframe#results").getByRole("button").boundingBox();
  await probe.close();
  return box;
}

export default async function (page, shot) {
  const target = await buttonBox(page);
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.click();
  await page.keyboard.type(process.env.CHECK_ASK);
  await page.keyboard.press("Enter");
  const deadline = Date.now() + 420000;
  let clicked = false;
  await page.waitForTimeout(3000);
  while (Date.now() < deadline) {
    if (await page.getByText("Done", { exact: true }).last().isVisible().catch(() => false)) break;
    const allow = page.getByRole("button", { name: ALLOW });
    if (await allow.count()) {
      const picker = page.getByRole("combobox", { name: "Permission duration" }).first();
      if (await picker.count()) {
        await picker.click();
        await page.getByRole("option", { name: "This session" }).click().catch(() => {});
      }
      await page.getByRole("button", { name: ALLOW }).first().click();
      await page.waitForTimeout(1500);
      continue;
    }
    const row = page.getByRole("button", { name: /^Open / }).filter({
      has: page.getByText("a.fixture.localhost", { exact: true }),
    });
    if (!clicked) {
      const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
      if (!(await row.first().isVisible().catch(() => false))) await layout.click().catch(() => {});
      if (await row.first().isVisible().catch(() => false)) {
        await row.first().click();
        const live = page.locator('[data-slot="browser-live-view"]').first();
        await live.locator("img").waitFor({ timeout: 20000 });
        await page.waitForTimeout(2500);
        const view = await live.boundingBox();
        const scale = Math.min(view.width / 1280, view.height / 800);
        const left = view.x + (view.width - 1280 * scale) / 2;
        const top = view.y + (view.height - 800 * scale) / 2;
        await page.mouse.click(
          left + (target.x + target.width / 2) * scale,
          top + (target.y + target.height / 2) * scale,
        );
        clicked = true;
        await page.waitForTimeout(1500);
        await shot("person-clicked");
        continue;
      }
    }
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(1500);
  await shot("answered");
  console.error(JSON.stringify({ clicked, url: page.url() }));
}
