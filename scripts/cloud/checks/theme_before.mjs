// Baseline screenshots of the Light and Dark themes before the Notebook theme
// work: a conversation with code and a table, Appearance settings, and the
// widget gallery's chart, workflow railroad, status and code components.
//
//   node scripts/cloud/ui_check.mjs --label before-wide --steps scripts/cloud/checks/theme_before.mjs
//   node scripts/cloud/ui_check.mjs --label before-phone --width 390 --height 844 --steps scripts/cloud/checks/theme_before.mjs
const PROMPT =
  "Without using any tools, reply with a short Python code block that computes the mean of a list, " +
  "then a markdown table of three planets with their radius in km.";

export default async function (page, shot) {
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.click();
  await page.keyboard.type(PROMPT);
  await page.keyboard.press("Enter");
  await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 120000 }).catch(() => {});
  await page.waitForTimeout(1500);
  const conversation = page.url();
  const origin = new URL(conversation).origin;

  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => localStorage.setItem("theme", t), theme);
    await page.goto(conversation, { waitUntil: "networkidle" });
    await page.waitForTimeout(2500);
    await shot(`${theme}-conversation`);
    await page.goto(`${origin}/settings/appearance`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1000);
    await shot(`${theme}-settings`);
    for (const slug of ["chart", "workflow", "status", "data-table"]) {
      await page.goto(`${origin}/widget-preview.html#${slug}`, { waitUntil: "networkidle" });
      await page.getByRole("tab", { name: "Components" }).click();
      await page.waitForTimeout(2000);
      await shot(`${theme}-gallery-${slug}`);
    }
  }
}
