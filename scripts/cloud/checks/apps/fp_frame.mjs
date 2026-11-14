// Friction polish (FP-D2, FP-D9): a view's label, its truthful state, and Reload view.
//
//   FP_SESSION  /workspaces/<w>/sessions/<s> of a conversation with a view
//   FP_TAB=1    (after reopening a closed view) choose "Open in tab" on the newest view card first
//   FP_RELOAD=1 open the frame's More menu and choose Reload view
//   FP_WAIT     seconds to let the view draw before reading its state (default 20)
// Prints FP_FRAME with each frame's header (label and state), its accessible name, the
// tab labels, and the state after a reload.
import { allowPending, heroShot } from "./_lib.mjs";

async function header(page) {
  return page
    .locator("[data-mcp-app]")
    .filter({ visible: true })
    .evaluateAll((frames) =>
      frames.map((frame) => ({
        header: frame.querySelector("[data-slot='frame-header'], header, div")?.innerText.replace(/\s+/gu, " ").trim(),
        title: frame.querySelector("iframe")?.getAttribute("title") || "",
        blank: Boolean(frame.querySelector("[data-mcp-app-blank]")),
      })),
    );
}

export default async function (page, shot) {
  const out = {};
  await page.goto(new URL(process.env.FP_SESSION, page.url()).toString(), { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(6000);
  // A view whose browser went away reads "View closed": reopen it first (MA19).
  const reopen = page.getByRole("button", { name: /^Reopen/ }).last();
  await reopen.waitFor({ timeout: 20000 }).catch(() => undefined);
  if (await reopen.count()) {
    out.reopenCard = (await reopen.locator("xpath=..").innerText()).replace(/\s+/gu, " ").trim();
    console.error(`[fp] reopen: ${out.reopenCard}`);
    await reopen.click();
    console.error('[fp] reopened');
    // A reopen replays the call through the person's permissions: allow it for the session.
    const sid = process.env.FP_SESSION.split("/").at(-1);
    out.allowed = [];
    for (let i = 0; i < 10; i += 1) {
      await allowPending(sid, out.allowed);
      await page.waitForTimeout(2000);
    }
    await page.waitForTimeout(10000);
  }
  if (process.env.FP_TAB === "1") {
    const open = page.getByRole("button", { name: "Open in tab" }).filter({ visible: true }).last();
    const show = page.getByRole("button", { name: "Show tab" }).filter({ visible: true }).last();
    if (await open.count()) await open.click();
    else if (await show.count()) await show.click();
  }
  await page.waitForTimeout(Number(process.env.FP_WAIT || 20) * 1000);
  out.tabs = await page
    .getByRole("tab")
    .evaluateAll((tabs) => tabs.map((tab) => tab.innerText.trim()).filter(Boolean));
  out.before = await header(page);
  await shot("frame");
  if (process.env.FP_RELOAD === "1") {
    const more = page.getByRole("button", { name: /^More actions for / }).filter({ visible: true }).last();
    console.error(`[fp] more buttons: ${await more.count()}`);
    await more.click();
    await page.waitForTimeout(800);
    await shot("frame-menu");
    await page.getByRole("menuitem", { name: /Reload view/ }).click();
    await page.waitForTimeout(1500);
    out.during = await header(page);
    await page.waitForTimeout(Number(process.env.FP_WAIT || 20) * 1000);
    out.after = await header(page);
    await shot("frame-reloaded");
  }
  if (process.env.FP_HERO === "1") await heroShot(page, shot, "hero");
  console.log(`FP_FRAME ${JSON.stringify(out)}`);
}
