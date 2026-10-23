// T7: under the Debug theme every color token is red and every typeface is a
// marker face, so an element that is not red, or not in the marker face, is
// not reading the core theme. Audits each screen's computed styles.
//
//   node scripts/cloud/ui_check.mjs --label t7-wide --steps scripts/cloud/checks/theme_t7_debug.mjs
import { appliedTheme, chooseTheme, debugAudit, GALLERY, openConversation, openFlowStates, openGallery, origin, quietReleaseChecks } from "./theme_lib.mjs";

export const setup = quietReleaseChecks;

export default async function (page, shot) {
  const report = [];
  const audit = async (screen, scheme) => {
    const result = await debugAudit(page);
    report.push({ screen, scheme, applied: await appliedTheme(page), checked: result.checked, offenders: result.offenders });
    await shot(`${scheme}-${screen}`);
  };
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    await chooseTheme(page, "Debug");
    await audit("settings", scheme);
    await openConversation(page);
    await audit("conversation", scheme);
    for (const slug of [...GALLERY, "metric", "steps", "callout", "diff"]) {
      await openGallery(page, slug);
      await audit(slug, scheme);
    }
    await openFlowStates(page);
    await audit("railroad", scheme);
    await page.goto(`${origin(page)}/runs`, { waitUntil: "load" });
    await page.waitForTimeout(2500);
    await audit("runs", scheme);
  }
  console.log(`T7 ${JSON.stringify(report)}`);
}
