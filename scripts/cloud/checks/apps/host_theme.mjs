// The host's theme reaches the view: CLIO sends `theme` and the standardized style
// variables from its notebook tokens, and resends them when the theme switches. Takes
// desktop hero shots (left navigation collapsed, canvas wide) in dark and light.
import { check, log, openConversation, sendAndSettle, viewIn } from "./_app_view.mjs";

async function viewColours(page) {
  const frame = page
    .frames()
    .find((f) => f.url() === "about:srcdoc" && f.parentFrame()?.url().includes("/mcp-apps/"));
  return frame.evaluate(() => {
    const body = getComputedStyle(document.body);
    return {
      background: body.backgroundColor,
      text: body.color,
      variable: getComputedStyle(document.documentElement)
        .getPropertyValue("--color-background-primary")
        .trim(),
    };
  });
}

async function setTheme(page, id, dark) {
  // What the app's own theme switch does to <html> (theme/preference.ts switchTheme).
  await page.evaluate(
    ([themeId, isDark]) => {
      document.documentElement.dataset.theme = themeId;
      document.documentElement.classList.toggle("dark", isDark);
    },
    [id, dark],
  );
  await page.waitForTimeout(1500);
}

export default async function (page, shot) {
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-theme");
  await sendAndSettle(page, "Open the host panel for the topic theme.");
  await page.getByRole("button", { name: "Open in tab" }).last().click();
  await page.locator("[data-mcp-app-tab] [data-mcp-app-iframe]").first().waitFor({ timeout: 60000 });
  await page.locator('[data-slot="sidebar-trigger"]').first().click().catch(() => {});
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" });
  const box = await handle.boundingBox().catch(() => null);
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(480, box.y + box.height / 2, { steps: 12 });
    await page.mouse.up();
  }
  await viewIn(page, "tab").locator("#mode").waitFor({ timeout: 30000 });
  await setTheme(page, "dark", true);
  const host = async () =>
    page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--background").trim(),
    );
  const dark = { host: await host(), view: await viewColours(page) };
  await shot("hero-dark");
  await setTheme(page, "light", false);
  const light = { host: await host(), view: await viewColours(page) };
  await shot("hero-light");
  log("dark", dark);
  log("light", light);
  check(dark.view.variable === dark.host, "dark: the view has CLIO's background token");
  check(light.view.variable === light.host, "light: the view has CLIO's background token");
  check(dark.view.background !== light.view.background, "the view followed the theme switch");
  check(!/rgb\(255, 255, 255\)/.test(light.view.background), "not plain white");
}
