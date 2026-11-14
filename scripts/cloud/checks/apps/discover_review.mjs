// Discover's cards and search on the review page (external/gact-tui/web/tests/review/
// discover-cards.html), with no CLIO stack: the production Extensions library over a
// fixture slice of the catalog. Serve the web app with Vite, then:
//
//   node scripts/cloud/checks/apps/discover_review.mjs --url http://127.0.0.1:5298 --out DIR
//
// For light and dark, at 1440x900 and 390x844, it shoots Discover, Discover with a query
// (the "Search the MCP Registry for …" row), and the Enable button of one card at device
// scale 2 (`enable-<theme>-<width>.png`) for the centring measurement. Prints one JSON
// line per run with console errors and failed requests.
import { createRequire } from "node:module";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import path from "node:path";

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const require = createRequire(path.join(root, "external/gact-tui/web/package.json"));
const { chromium } = require("@playwright/test");
const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
};
const base = arg("url", "http://127.0.0.1:5298");
const out = arg("out", path.join(root, ".cloud-run/shots"));
mkdirSync(out, { recursive: true });

/** The image's own Chromium, as ui_check.mjs finds it. */
function chromiumPath() {
  const dir = "/opt/pw-browsers";
  if (!existsSync(dir)) return undefined;
  if (existsSync(path.join(dir, "chromium"))) return path.join(dir, "chromium");
  for (const d of readdirSync(dir).filter((d) => /^chromium-\d+$/u.test(d)).sort().reverse())
    for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"])
      if (existsSync(path.join(dir, d, sub))) return path.join(dir, d, sub);
  return undefined;
}
const args = ["--enable-unsafe-swiftshader"];
const browser = await chromium
  .launch({ args })
  .catch(() => chromium.launch({ args, executablePath: chromiumPath() }));
for (const theme of ["light", "dark"]) {
  for (const [width, height, label] of [
    [1440, 900, "desktop"],
    [390, 844, "phone"],
  ]) {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 2,
      colorScheme: theme,
      ...(label === "phone" ? { hasTouch: true, isMobile: true } : {}),
    });
    const page = await context.newPage();
    const errors = [];
    const failed = [];
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("requestfailed", (r) => failed.push(`${r.url()} ${r.failure()?.errorText}`));
    const query = `theme=${theme}${label === "phone" ? "&width=phone" : ""}`;
    await page.goto(`${base}/tests/review/discover-cards.html?${query}`);
    await page.locator('[role="radio"]:visible', { hasText: /^Discover$/u }).first().click();
    await page.locator('[data-slot="discover-card"]').first().waitFor({ timeout: 30000 });
    await page.waitForTimeout(2500);
    const name = (what) => path.join(out, `dc-review-${what}-${theme}-${label}.png`);
    await page.screenshot({ path: name("discover") });
    const enable = page.locator('[data-slot="discover-card-enable"]').first();
    await enable.screenshot({ path: path.join(out, `enable-${theme}-${label}.png`) });
    const box = await enable.boundingBox();
    const field = page.locator("input[data-extension-search]:visible").first();
    const placeholder = await field.getAttribute("placeholder");
    await field.fill("hdf");
    await page.waitForTimeout(1500);
    await page.mouse.move(width - 4, height - 4);
    await page.screenshot({ path: name("search") });
    const registryRow = await page.locator('[data-slot="registry-search"]').innerText();
    const clear = await page.getByRole("button", { name: "Clear search" }).count();
    const layout = await page.evaluate(() => {
      const rect = (el) => el?.getBoundingClientRect();
      const tabs = rect(document.querySelector('[role="toolbar"] [role="radiogroup"]'));
      const search = rect(document.querySelector("form[role=search]"));
      return {
        sameRow: Boolean(tabs && search && Math.abs(tabs.top - search.top) < 8),
        searchWidth: Math.round(search?.width ?? 0),
      };
    });
    console.log(
      JSON.stringify({
        theme,
        label,
        placeholder,
        registryRow,
        clear,
        layout,
        enableBox: box,
        consoleErrors: errors,
        failedRequests: failed,
      }),
    );
    await context.close();
  }
}
await browser.close();
