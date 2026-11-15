// Hero captures of a finished Helene walkthrough (helene_hdf5.mjs) for the website example:
// the left navigation collapsed and the workbench maximized, so each view fills the frame.
//
//   WS=<workspace> SID=<session> node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_hero.mjs --label hero --width 1440 --height 900 \
//     --scale 2 [--theme dark]
//
// It reopens what the conversation made, as a person returning to it would: the /MSL field
// from the person's message (Helene in the Gulf, 26 Sep 12 UTC), the ParaView view from its
// card, and the latest dashboard from its card. No request is sent.

async function maximize(page) {
  await page.getByRole("button", { name: "Maximize canvas" }).click({ timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(1500);
}

async function restore(page) {
  await page
    .getByRole("button", { name: "Restore canvas beside conversation" })
    .click({ timeout: 5000 })
    .catch(() => {});
  await page.waitForTimeout(1000);
}

async function field(page, shot) {
  await page.getByRole("button", { name: /^Open referenced local file msl_2024-09\.nc › \/MSL$/ }).first().click();
  await page.waitForTimeout(6000);
  // Set the view up beside the conversation, where its controls fold into More controls,
  // then give it the whole frame.
  const viewer = page.frameLocator('iframe[title^="HDF5 viewer"]');
  const press = async (name) => {
    for (const where of [page, viewer]) {
      const button = where.getByRole("button", { name, exact: true }).first();
      if (await button.isVisible({ timeout: 1500 }).catch(() => false)) {
        await button.click({ timeout: 3000 });
        return true;
      }
    }
    console.log(`STEP_SKIPPED ${name}`);
    return false;
  };
  await press("Image");
  await page.waitForTimeout(4000);
  if (await press("More controls")) await press("Flip Y");
  await page.keyboard.press("Escape").catch(() => {});
  // 26 Sep 12 UTC is step 612 of 720: drag the time slider near it, then step with the
  // arrow keys (each step draws a slice, so a long run of keys is avoided).
  const slider = viewer.locator('input[type="range"]').last();
  const box = await slider.boundingBox({ timeout: 5000 });
  if (box) {
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + 2, y);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.85, y, { steps: 20 });
    await page.mouse.up();
    await page.waitForTimeout(1500);
  }
  for (let k = 0; k < 120; k += 1) {
    const now = Number(await slider.evaluate((el) => el.value));
    if (now === 612) break;
    await page.keyboard.press(now < 612 ? "ArrowRight" : "ArrowLeft");
    await page.waitForTimeout(60);
  }
  await page.waitForTimeout(3000);
  await maximize(page);
  // With room for every control, keep the grid's aspect ratio (0.25 degrees each way).
  await viewer.getByRole("button", { name: /Keep ratio/ }).first().click({ timeout: 3000 }).catch(() => {});
  await page.waitForTimeout(5000);
  await page.mouse.move(1430, 880); // the empty foot of the right rail: no hover
  await shot("hero-field");
  await restore(page);
}

/** The live view's own page: the frame that holds its Camera button. */
async function appFrame(page) {
  for (let k = 0; k < 30; k += 1) {
    for (const frame of page.frames()) {
      if (await frame.getByRole("button", { name: "Camera" }).count().catch(() => 0)) return frame;
    }
    await page.waitForTimeout(2000);
  }
  throw new Error("the live 3D view did not load");
}

async function paraview(page, shot) {
  const tab = page.getByRole("tab", { name: /^ParaView/ }).first();
  if (await tab.isVisible().catch(() => false)) await tab.click();
  else await page.getByRole("button", { name: "Reopen" }).first().click();
  await page.waitForTimeout(3000);
  await maximize(page);
  // The view as the agent left it (the conversation's last request set its camera).
  await appFrame(page);
  await page.waitForTimeout(15000);
  await page.mouse.move(1430, 880); // the empty foot of the right rail: no hover
  await shot("hero-paraview");
  await restore(page);
}

async function dashboard(page, shot) {
  await page.getByRole("button", { name: /^Open Hurricane Helene, 24-27 Sep 2024/ }).last().click();
  await page.waitForTimeout(4000);
  await maximize(page);
  await page.waitForTimeout(12000);
  await page.mouse.move(1430, 880); // the empty foot of the right rail: no hover
  await shot("hero-dashboard");
  const live = page.locator('iframe[title*="ParaView"]').locator("visible=true").first();
  if (await live.count()) {
    await live.scrollIntoViewIfNeeded();
    await page.waitForTimeout(10000);
    await shot("hero-dashboard-3d");
  }
  await restore(page);
}

export default async function (page, shot) {
  await page.goto(
    new URL(`/workspaces/${process.env.WS}/sessions/${process.env.SID}`, page.url()).toString(),
    { waitUntil: "domcontentloaded", timeout: 120000 },
  );
  await page.locator('textarea, [contenteditable="true"]').last().waitFor({ timeout: 180000 });
  await page.waitForTimeout(4000);
  if (await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "Toggle Sidebar" }).first().click();
    await page.waitForTimeout(800);
  }
  const only = (process.env.HERO || "field,paraview,dashboard").split(",");
  if (only.includes("field")) await field(page, shot);
  if (only.includes("paraview")) await paraview(page, shot);
  if (only.includes("dashboard")) await dashboard(page, shot);
}
