// Friction polish (FP-D6): the region comment editor opens on the comment, clear of the
// "Add N marks to message" tray, with the region's numbers behind "Adjust region".
//
//   FP_SESSION  /workspaces/<w>/sessions/<s> whose newest view is in a tab (or "Show tab")
//   FP_REGION   "x,y,w,h" fractions of the view (default: a region at the bottom right,
//               where the editor used to cover the tray)
// Prints FP_COMMENT with the editor's and the tray's boxes, whether they overlap, which
// element has the focus, and whether the region numbers show before and after the disclosure.
import { allowPending } from "./_lib.mjs";

const rect = async (locator) => (await locator.count()) ? locator.first().boundingBox() : null;
const overlap = (a, b) =>
  Boolean(a && b) && a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

export default async function (page, shot) {
  const phone = page.viewportSize().width < 600;
  const out = {};
  await page.goto(new URL(process.env.FP_SESSION, page.url()).toString(), { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(6000);
  // A view whose browser went away reads "View closed": reopen it (allowed for the session).
  const reopen = page.getByRole("button", { name: /^Reopen/ }).filter({ visible: true }).last();
  await reopen.waitFor({ timeout: 15000 }).catch(() => undefined);
  if (await reopen.count()) {
    await reopen.click();
    const log = [];
    for (let i = 0; i < 8; i += 1) {
      await page.waitForTimeout(2000);
      await allowPending(process.env.FP_SESSION.split("/").at(-1), log);
    }
  }
  const shown = await page.locator("[data-mcp-app-tab]").filter({ visible: true }).count();
  console.error(`[fp] tab shown already: ${shown}`);
  for (const name of shown ? [] : ["Open in tab", "Show tab"]) {
    const button = page.getByRole("button", { name }).filter({ visible: true }).last();
    if (await button.count()) {
      await button.click();
      break;
    }
  }
  if (!phone) {
    const sidebar = page.getByRole("button", { name: "Toggle Sidebar" }).first();
    if (await sidebar.count()) await sidebar.click().catch(() => undefined);
    const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
    if (await maximize.count()) await maximize.click().catch(() => undefined);
  }
  await page.waitForTimeout(20000);
  const frame = page.locator("[data-mcp-app-tab]").filter({ visible: true }).last();
  console.error(`[fp] tab frames: ${await frame.count()}, comments buttons: ${await frame.getByRole("button", { name: "Comments" }).count()}`);
  await frame.getByRole("button", { name: "Comments" }).first().click({ force: phone });
  await page.waitForTimeout(1500);
  const box = await frame.locator('[data-slot="comment-region-layer"]').boundingBox();
  const [x, y, w, h] = (process.env.FP_REGION || "0.5,0.45,0.3,0.4").split(",").map(Number);
  await page.mouse.move(box.x + x * box.width, box.y + y * box.height);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(box.x + (x + (w * i) / 12) * box.width, box.y + (y + (h * i) / 12) * box.height);
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
  const editor = page.locator('[data-slot="comment-editor"]');
  await editor.waitFor({ timeout: 20000 });
  await page.waitForTimeout(1500);
  out.header = (await editor.locator("p").first().innerText()).replace(/\s+/gu, " ");
  out.focus = await page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
  out.numbersShown = await editor.getByText("Region (% of view)").isVisible();
  out.editor = await rect(editor);
  out.tray = await rect(page.locator('[data-slot="comment-controls"]'));
  out.overlap = overlap(out.editor, out.tray);
  await page.keyboard.type("Why is this corner darker?");
  await shot("editor");
  await editor.getByText("Adjust region").click();
  await page.waitForTimeout(600);
  out.numbersAfterAdjust = await editor.getByText("Region (% of view)").isVisible();
  await shot("editor-adjust");
  await editor.getByRole("button", { name: /^Cancel$/ }).click();
  console.log(`FP_COMMENT ${JSON.stringify(out)}`);
}
