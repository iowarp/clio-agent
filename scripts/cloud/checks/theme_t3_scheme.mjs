// T3: Notebook follows the system scheme, live and across reloads, and a
// reload shows no frame of the other variant. Every change to <html>'s theme
// attributes and every paint entry is recorded from the start of the document.
//
//   node scripts/cloud/ui_check.mjs --label t3-wide --steps scripts/cloud/checks/theme_t3_scheme.mjs
import { appliedTheme, chooseTheme, openConversation, quietReleaseChecks } from "./theme_lib.mjs";

export const setup = quietReleaseChecks;

export default async function (page, shot) {
  await page.addInitScript(() => {
    const states = (window.__themeStates = []);
    const record = (event, at = performance.now()) => {
      const root = document.documentElement;
      states.push({
        event,
        at: Math.round(at * 10) / 10,
        theme: root?.dataset.theme ?? null,
        dark: root?.classList.contains("dark") ?? false,
      });
    };
    record("document");
    new MutationObserver(() => record("html-attributes")).observe(document, {
      subtree: true,
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    });
    new PerformanceObserver((list) => list.getEntries().forEach((entry) => record(entry.name, entry.startTime))).observe({
      type: "paint",
      buffered: true,
    });
  });
  const results = { live: [], reload: [] };
  await page.emulateMedia({ colorScheme: "light" });
  await chooseTheme(page, "Notebook");
  results.live.push({ system: "light", applied: await appliedTheme(page) });
  const origin = await page.evaluate(() => performance.timeOrigin);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.waitForTimeout(800);
  results.live.push({ system: "dark", applied: await appliedTheme(page) });
  await shot("settings-system-dark");
  await page.emulateMedia({ colorScheme: "light" });
  await page.waitForTimeout(800);
  results.live.push({ system: "light", applied: await appliedTheme(page) });
  results.liveWithoutReload = (await page.evaluate(() => performance.timeOrigin)) === origin;

  await openConversation(page);
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.reload({ waitUntil: "networkidle" });
    await page.locator("pre, table").first().waitFor({ timeout: 20000 }).catch(() => {});
    const states = await page.evaluate(() => window.__themeStates);
    const firstPaint = states.find((state) => state.event === "first-paint");
    const expected = { theme: "notebook", dark: scheme === "dark" };
    // Once the boot script has run, no recorded state may show another theme or variant.
    const booted = states.filter((state) => state.theme !== null);
    const otherVariant = booted.filter((state) => state.theme !== expected.theme || state.dark !== expected.dark);
    const finalState = await appliedTheme(page);
    results.reload.push({
      system: scheme,
      expected,
      themeAppliedAt: booted[0]?.at,
      atFirstPaint: firstPaint && { theme: firstPaint.theme, dark: firstPaint.dark, at: firstPaint.at },
      final: finalState,
      statesShowingAnotherVariant: otherVariant.length,
      timeline: states,
    });
    await shot(`reload-system-${scheme}`);
  }
  console.log(`T3 ${JSON.stringify(results, null, 2)}`);
}
