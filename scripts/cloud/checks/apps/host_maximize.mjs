// Maximizing or restoring the canvas, and on a phone closing and reopening the canvas
// sheet, keeps an app view in its tab alive: no frame navigations, and the view's own
// state (its Loaded time and its last action) is unchanged. Run at 1440x900 and 390x844.
import {
  check,
  expectText,
  log,
  openConversation,
  sendAndSettle,
  viewIn,
} from "./_app_view.mjs";

export default async function (page, shot) {
  const phone = page.viewportSize().width < 900;
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-maximize");
  await sendAndSettle(page, "Open the host panel for the topic epsilon.");
  await viewIn(page, "inline").locator("#fullscreen").click();
  const tab = viewIn(page, "tab");
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  await tab.locator("#context").click();
  await expectText(tab.locator("#status"), /context reported/);
  const loaded = await tab.locator("#loaded").textContent();
  const navigations = [];
  let phase = "settled";
  page.on("framenavigated", (frame) => {
    if (frame.url() === "about:srcdoc" || /\/mcp-apps\//.test(frame.url()))
      navigations.push(`${phase}: ${frame.url().slice(0, 60)}`);
  });
  const state = async () => ({
    loaded: await tab.locator("#loaded").textContent(),
    status: await tab.locator("#status").textContent(),
  });
  phase = "maximize";
  await page.getByRole("button", { name: "Maximize canvas" }).first().click();
  await page.waitForTimeout(4000);
  await shot("maximized");
  const maximized = await state();
  phase = "restore";
  await page.getByRole("button", { name: "Restore canvas beside conversation" }).first().click();
  await page.waitForTimeout(4000);
  await shot("restored");
  const restored = await state();
  let reopened;
  if (phone) {
    phase = "sheet-close";
    await page.getByRole("button", { name: "Close workspace canvas" }).first().click();
    await page.waitForTimeout(2500);
    await shot("sheet-closed");
    phase = "sheet-open";
    await page.getByRole("button", { name: "Open workspace canvas" }).first().click();
    await page.waitForTimeout(4000);
    await shot("sheet-reopened");
    reopened = await state();
  }
  log("view state", { loaded, maximized, restored, reopened, navigations });
  check(navigations.length === 0, "no frame navigated");
  for (const seen of [maximized, restored, reopened].filter(Boolean)) {
    check(seen.loaded === loaded, "the view did not reload");
    check(/context reported/.test(seen.status), "the view kept its state");
  }
}
