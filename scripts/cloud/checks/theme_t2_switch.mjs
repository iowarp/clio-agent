// T2: switching Light, Dark and Notebook in Settings changes every screen with
// no reload and no leftover colors. Each switch happens in place; the screen's
// computed colors are then compared with the same screen after a reload.
//
//   node scripts/cloud/ui_check.mjs --label t2-wide --steps scripts/cloud/checks/theme_t2_switch.mjs
import { appliedTheme, chooseTheme, sidebarLink, openConversation, snapshotDiff, styleSnapshot, VARIANTS, quietReleaseChecks } from "./theme_lib.mjs";

export const setup = quietReleaseChecks;

export default async function (page, shot) {
  page.on("response", (r) => r.status() >= 400 && console.log(`HTTP ${r.status()} ${r.url()}`));
  const results = [];
  await openConversation(page);
  const conversation = page.url();
  for (const variant of VARIANTS) {
    await page.emulateMedia({ colorScheme: variant.scheme });
    // Go to Settings inside the app (a client-side route), switch, and come back.
    // A reload starts a new document with a new time origin.
    const document = await page.evaluate(() => performance.timeOrigin);
    await sidebarLink(page, "Settings");
    await chooseTheme(page, variant.choice);
    await shot(`${variant.name}-settings`);
    await page.goBack();
    await page.goBack();
    await page.locator("pre, table").first().waitFor({ timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const noReload = (await page.evaluate(() => performance.timeOrigin)) === document;
    const applied = await appliedTheme(page);
    const inPlace = await styleSnapshot(page);
    await shot(`${variant.name}-conversation-in-place`);
    await page.goto(conversation, { waitUntil: "networkidle" });
    await page.locator("pre, table").first().waitFor({ timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const reloaded = await styleSnapshot(page);
    const leftovers = snapshotDiff(inPlace, reloaded);
    results.push({
      variant: variant.name,
      noReload,
      applied,
      expected: { theme: variant.id, dark: variant.dark },
      elementsCompared: Object.keys(reloaded).length,
      leftovers: leftovers.slice(0, 10),
      leftoverCount: leftovers.length,
    });
  }
  console.log(`T2 ${JSON.stringify(results, null, 2)}`);
}
