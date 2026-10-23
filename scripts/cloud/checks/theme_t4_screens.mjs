// T4: a conversation with code and a table, a chart, a workflow, status chips,
// a data table, code and the workflow railroad, in Light, Dark and both
// Notebook variants. Theme switching itself is T2; this sets the stored choice.
//
//   node scripts/cloud/ui_check.mjs --label t4-wide --steps scripts/cloud/checks/theme_t4_screens.mjs
import { appliedTheme, GALLERY, openConversation, openFlowStates, openGallery, VARIANTS, quietReleaseChecks } from "./theme_lib.mjs";

export const setup = quietReleaseChecks;

export default async function (page, shot) {
  const results = [];
  for (const variant of VARIANTS) {
    await page.emulateMedia({ colorScheme: variant.scheme });
    await page.evaluate((theme) => {
      const record = JSON.parse(localStorage.getItem("clio.appearance.v1") ?? "{}");
      localStorage.setItem("clio.appearance.v1", JSON.stringify({ ...record, theme }));
    }, variant.id);
    await openConversation(page);
    const applied = await appliedTheme(page);
    await shot(`${variant.name}-conversation`);
    for (const slug of GALLERY) {
      await openGallery(page, slug);
      await shot(`${variant.name}-${slug}`);
    }
    await openFlowStates(page);
    const railroad = await page.evaluate(() =>
      [...document.querySelectorAll(".clio-flow-diagram [class*='st-']")].map((node) => {
        const shape = node.querySelector("rect, polygon, path");
        const style = shape && getComputedStyle(shape);
        return { state: [...node.classList].find((c) => c.startsWith("st-")), fill: style?.fill, stroke: style?.stroke };
      }),
    );
    await shot(`${variant.name}-railroad`);
    results.push({ variant: variant.name, applied, railroad });
  }
  console.log(`T4 ${JSON.stringify(results, null, 2)}`);
}
