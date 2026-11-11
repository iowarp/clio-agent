// W4 check: the agent needs a signed-in page; the person signs in through the live view.
// 1. CHECK_ASK is sent and questions are answered (browser_shared_tab.mjs).
// 2. The page is opened from the Work rail, and the person clicks the username and
//    password fields and types into them on the live frame (coordinates are measured on
//    a copy of the fixture's sign-in page at the shared page's 1280 px width).
// 3. CHECK_FOLLOW_UP is sent and answered the same way.
import shared from "./browser_shared_tab.mjs";

const SIGNIN = process.env.CHECK_SIGNIN_URL || "http://a.fixture.localhost:8790/signin";

async function fieldBoxes(page) {
  const probe = await page.context().browser().newPage();
  await probe.setViewportSize({ width: 1280, height: 800 });
  await probe.goto(SIGNIN);
  const box = async (selector) => probe.locator(selector).boundingBox();
  const boxes = {
    user: await box("input[name=username]"),
    pass: await box("input[type=password]"),
  };
  await probe.close();
  return boxes;
}

function toClient(view, point) {
  const scale = Math.min(view.width / 1280, view.height / 800);
  const left = view.x + (view.width - 1280 * scale) / 2;
  const top = view.y + (view.height - 800 * scale) / 2;
  return { x: left + point.x * scale, y: top + point.y * scale };
}

export default async function (page, shot) {
  if (process.env.CHECK_SKIP_FIRST !== "1") await shared(page, shot);
  else if (process.env.CHECK_SESSION_PATH) {
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  const boxes = await fieldBoxes(page);
  const host = page.getByRole("button", { name: /^Open / }).filter({
    has: page.getByText("a.fixture.localhost", { exact: true }),
  });
  const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
  for (let i = 0; i < 4 && !(await host.first().isVisible().catch(() => false)); i += 1) {
    await layout.click();
    await page.waitForTimeout(1000);
  }
  await host.first().click();
  const live = page.locator('[data-slot="browser-live-view"]').first();
  await live.locator("img").waitFor({ timeout: 20000 });
  await page.waitForTimeout(1500);
  await shot("live-signin-page");
  const view = await live.boundingBox();
  const center = (b) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 });
  const user = toClient(view, center(boxes.user));
  await page.mouse.click(user.x, user.y);
  await page.keyboard.type("ada", { delay: 60 });
  const pass = toClient(view, center(boxes.pass));
  await page.mouse.click(pass.x, pass.y);
  await page.keyboard.type("correct horse", { delay: 60 });
  await page.keyboard.press("Enter");
  await page.waitForTimeout(3000);
  await shot("live-signed-in");
  process.env.CHECK_ASK = process.env.CHECK_FOLLOW_UP || "I have signed in. Please continue.";
  process.env.CHECK_SESSION_PATH = new URL(page.url()).pathname;
  await shared(page, shot);
}
