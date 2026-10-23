// T5: measured WCAG contrast of every text and tint pair in each theme
// variant, from the colors the browser actually resolves. Body text needs
// 4.5:1; the status icon on the page background needs 3:1.
//
//   node scripts/cloud/ui_check.mjs --label t5 --steps scripts/cloud/checks/theme_t5_contrast.mjs
import { appliedTheme, contrastPairs, origin, VARIANTS, quietReleaseChecks } from "./theme_lib.mjs";

const TEXT = 4.5;
const ICON = 3;
const PAIRS = [
  // Surfaces and ink
  ["foreground", "background", TEXT],
  ["card-foreground", "card", TEXT],
  ["popover-foreground", "popover", TEXT],
  ["foreground", "muted", TEXT],
  ["secondary-foreground", "secondary", TEXT],
  ["sidebar-foreground", "sidebar", TEXT],
  ["muted-foreground", "background", TEXT],
  ["muted-foreground", "card", TEXT],
  ["muted-foreground", "muted", TEXT],
  // Accent
  ["primary-foreground", "primary", TEXT],
  ["accent-foreground", "accent", TEXT],
  ["sidebar-accent-foreground", "sidebar-accent", TEXT],
  ["link", "background", TEXT],
  ["link", "card", TEXT],
  // Science states: label on its tint, icon on the page
  ...["supported", "running", "refuted", "superseded"].flatMap((state) => [
    [`status-${state}-foreground`, `status-${state}`, TEXT],
    [`status-${state}-foreground`, "card", ICON],
  ]),
  // Outcomes
  ["destructive", "background", TEXT],
  ["destructive-foreground", "card", TEXT],
  ["success-foreground", "card", TEXT],
  ["info-foreground", "card", TEXT],
  ["warning-foreground", "card", TEXT],
  ["success", "background", TEXT],
  ["info", "background", TEXT],
  ["warning", "background", ICON],
  ["action", "background", ICON],
  ["invert-foreground", "invert", TEXT],
  // Terminals, code and highlights
  ["terminal-foreground", "terminal", TEXT],
  ["terminal-muted", "terminal", TEXT],
  ["terminal-accent", "terminal", TEXT],
  ["syntax-foreground", "syntax-background", TEXT],
  ["syntax-token-keyword", "syntax-background", TEXT],
  ["syntax-token-string", "syntax-background", TEXT],
  ["syntax-token-comment", "syntax-background", TEXT],
  ["syntax-token-function", "syntax-background", TEXT],
  ["highlight-foreground", "highlight", TEXT],
].map(([text, on, min]) => ({ text, on, min }));

export const setup = quietReleaseChecks;

export default async function (page, shot) {
  const report = [];
  for (const variant of VARIANTS) {
    await page.emulateMedia({ colorScheme: variant.scheme });
    await page.evaluate((theme) => {
      const record = JSON.parse(localStorage.getItem("clio.appearance.v1") ?? "{}");
      localStorage.setItem("clio.appearance.v1", JSON.stringify({ ...record, theme }));
    }, variant.id);
    await page.goto(`${origin(page)}/settings/appearance`, { waitUntil: "load" });
    await page.waitForTimeout(1500);
    const rows = await contrastPairs(page, PAIRS);
    report.push({ variant: variant.name, applied: await appliedTheme(page), rows });
    await shot(`${variant.name}`);
  }
  console.log(`T5 ${JSON.stringify(report)}`);
}
