// W16 check: the live view on a phone. A 390 x 844 touch context opens the conversation
// (CHECK_SESSION_PATH, which has a page on the fixture's /frames), opens the page from the
// Work rail, and taps the button inside the frame (its position measured on a copy of the
// fixture page). The desktop window then opens the same page with the canvas maximized,
// so the button's new label can be read on the shot.
import surface from "./browser_surface.mjs";

const FRAMES = process.env.CHECK_FRAMES_URL || "http://a.fixture.localhost:8790/frames";
const SHOTS = process.env.CHECK_SHOT_DIR || ".cloud-run/shots";

async function buttonBox(browser) {
  const probe = await browser.newPage();
  await probe.setViewportSize({ width: 1280, height: 800 });
  await probe.goto(FRAMES);
  const box = await probe.frameLocator("iframe#results").getByRole("button").boundingBox();
  await probe.close();
  return box;
}

export default async function (page, shot) {
  const browser = page.context().browser();
  const target = await buttonBox(browser);
  const phone = await browser.newPage({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });
  await phone.goto(page.url());
  await phone.waitForTimeout(2000);
  let n = 0;
  const phoneShot = async (name) => {
    n += 1;
    await phone.screenshot({ path: `${SHOTS}/w16-phone-${n}-${name}.png` });
  };
  await surface(phone, phoneShot);
  const live = phone.locator('[data-slot="browser-live-view"]').first();
  const view = await live.boundingBox();
  const scale = Math.min(view.width / 1280, view.height / 800);
  const left = view.x + (view.width - 1280 * scale) / 2;
  const top = view.y + (view.height - 800 * scale) / 2;
  const point = {
    x: left + (target.x + target.width / 2) * scale,
    y: top + (target.y + target.height / 2) * scale,
  };
  await phone.touchscreen.tap(point.x, point.y);
  await phone.waitForTimeout(2500);
  await phoneShot("after-tap");
  await phone.close();
  await surface(page, shot); // CHECK_SESSION_PATH again, in the desktop window
  console.error(JSON.stringify({ tapped: point, view, scale }));
}
