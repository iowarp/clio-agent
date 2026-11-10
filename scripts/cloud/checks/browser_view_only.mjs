// W10 check: a second UI window on a shared page is view only.
// 1. CHECK_ASK opens a page (answered by browser_shared_tab.mjs) and this window opens it
//    from the Work rail, so it is the first viewer and controls the page.
// 2. A second window opens the same conversation and page; its surface must say "View
//    only", and a click on the frame's button there must not reach the page.
// 3. The first window's live view is captured after the second window's click.
import shared from "./browser_shared_tab.mjs";

const FRAMES = process.env.CHECK_FRAMES_URL || "http://a.fixture.localhost:8790/frames";

async function openFromRail(page) {
  const row = page.getByRole("button", { name: /^Open / }).filter({
    has: page.getByText("a.fixture.localhost", { exact: true }),
  });
  const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
  for (let i = 0; i < 4 && !(await row.first().isVisible().catch(() => false)); i += 1) {
    await layout.click().catch(() => {});
    await page.waitForTimeout(1000);
  }
  await row.first().click();
  const live = page.locator('[data-slot="browser-live-view"]').first();
  await live.locator("img").waitFor({ timeout: 20000 });
  await page.waitForTimeout(2000);
  return live;
}

async function buttonBox(browser) {
  const probe = await browser.newPage();
  await probe.setViewportSize({ width: 1280, height: 800 });
  await probe.goto(FRAMES);
  const box = await probe.frameLocator("iframe#results").getByRole("button").boundingBox();
  await probe.close();
  return box;
}

export default async function (page, shot) {
  await shared(page, shot);
  const target = await buttonBox(page.context().browser());
  await openFromRail(page);
  await shot("first-window");
  const second = await page.context().browser().newPage({ viewport: { width: 1440, height: 900 } });
  await second.goto(page.url());
  await second.waitForTimeout(4000);
  const live = await openFromRail(second);
  const viewOnly = await second.getByText("View only").first().isVisible().catch(() => false);
  const label = await live.getAttribute("aria-label").catch(() => "");
  const view = await live.boundingBox();
  const scale = Math.min(view.width / 1280, view.height / 800);
  const left = view.x + (view.width - 1280 * scale) / 2;
  const top = view.y + (view.height - 800 * scale) / 2;
  await second.mouse.click(
    left + (target.x + target.width / 2) * scale,
    top + (target.y + target.height / 2) * scale,
  );
  await second.waitForTimeout(2000);
  await second.screenshot({ path: process.env.CHECK_SECOND_SHOT || ".cloud-run/shots/w10-second.png" });
  await page.waitForTimeout(1500);
  await shot("first-after-second-click");
  await second.close();
  console.error(JSON.stringify({ viewOnly, label }));
}
