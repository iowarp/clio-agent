// Phone layout with an app tab (run at --width 390 --height 844): the view in its
// tab docks between the conversation's header and its composer, so the person can
// type while the app is shown, and "Return to conversation" stays reachable.
import {
  check,
  expectText,
  log,
  openConversation,
  sendAndSettle,
  viewIn,
} from "./_app_view.mjs";

export default async function (page, shot) {
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-panel-dock");
  await sendAndSettle(page, "Open the host panel for the topic delta.");
  await viewIn(page, "inline").locator("#fullscreen").click();
  const tab = viewIn(page, "tab");
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  const sheet = page.locator('[data-slot="sheet-content"][data-app-dock]');
  await sheet.waitFor({ timeout: 10000 });
  await page.waitForTimeout(800);
  const composer = page.locator('[data-slot="clio-composer-stack"]').first();
  const header = page.locator('[data-slot="conversation-surface"] > *').first();
  const boxes = {
    sheet: await sheet.boundingBox(),
    composer: await composer.boundingBox(),
    header: await header.boundingBox(),
  };
  log("boxes", boxes);
  check(boxes.sheet.y + boxes.sheet.height <= boxes.composer.y + 1, "the composer is below the app");
  check(boxes.sheet.y >= boxes.header.y + boxes.header.height - 1, "the header is above the app");
  await shot("docked");
  const field = page.locator('textarea, [contenteditable="true"]').last();
  await field.click();
  await page.keyboard.type("Typing while the app is shown");
  const typed = await field.evaluate((el) => ("value" in el ? el.value : el.textContent));
  log("typed", typed);
  check(/Typing while the app is shown/.test(typed), "the composer takes text");
  check((await sheet.count()) === 1, "the app tab stays open while typing");
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  await shot("typing");
  await page.getByRole("button", { name: "Return to conversation" }).click();
  await expectText(viewIn(page, "inline").locator("#mode"), /^inline$/);
  await shot("returned");
}
