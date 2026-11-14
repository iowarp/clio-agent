// View call fixes (VF, MC-F3): a person reads a long paper in the PDF viewer, paging through
// it and moving the view between its card, its tab and the maximized canvas, so the view
// reads its file again on every mount and keeps polling. The store and the transcript must
// stay small (measure with store_usage.py before and after).
//
//   VF_SESSION  "<workspace id>/<session id>" whose latest view is the PDF viewer
//   VF_PAGES    how many times to press the view's next-page button (default 45)
//   VF_MOUNTS   how many Return / Open in tab rounds to make (default 3)
//   VF_PHONE=1  phone layout: no tab rounds or hero shot, paging in the card or the sheet
// Prints one JSON line: the page the view shows at the end, the mounts and the frames.
import { appFrames, heroShot, isViewDocument, showLastApp } from "./_lib.mjs";

const sleep = (page, ms) => page.waitForTimeout(ms);

async function viewDocument(page) {
  for (let i = 0; i < 60; i += 1) {
    for (const frame of page.frames().filter(isViewDocument)) {
      if (await frame.locator("#next-btn").count().catch(() => 0)) return frame;
    }
    await sleep(page, 1000);
  }
  throw new Error("no PDF view document with a next-page button");
}

async function pageState(frame) {
  const input = await frame.locator("#page-input").inputValue().catch(() => "");
  const total = await frame.locator("#total-pages").innerText().catch(() => "");
  return `${input} ${total}`.trim();
}

async function openInTab(page) {
  await page.getByRole("button", { name: /^(Open in tab|Show tab)$/ }).filter({ visible: true }).last().click({ timeout: 30000 });
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 60000 });
  await sleep(page, 6000);
}

async function returnToCard(page) {
  await page.getByRole("button", { name: "Return to conversation" }).filter({ visible: true }).first().click({ timeout: 20000 });
  await sleep(page, 6000);
}

export default async function (page, shot) {
  const [workspace, session] = process.env.VF_SESSION.split("/");
  const pages = Number(process.env.VF_PAGES || 45);
  const rounds = Number(process.env.VF_MOUNTS || 3);
  const phone = process.env.VF_PHONE === "1";
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await sleep(page, 8000);
  await showLastApp(page, await appFrames(page));
  let mounts = 1;
  // On a phone a view left in its tab shows as the docked sheet: "Show tab" opens it.
  if (!phone || (await page.getByRole("button", { name: "Show tab" }).filter({ visible: true }).count())) {
    await openInTab(page);
    mounts += 1;
  }
  let frame = await viewDocument(page);
  await frame.locator("#total-pages").filter({ hasNotText: /of 1$/ }).waitFor({ timeout: 90000 }).catch(() => undefined);
  const start = await pageState(frame);
  const states = [];
  for (let i = 0; i < pages; i += 1) {
    await frame.locator("#next-btn").click({ timeout: 10000 });
    await sleep(page, 700);
    if ((i + 1) % 15 === 0) states.push(await pageState(frame));
    if (i + 1 === 20) await shot("paged-20");
  }
  await shot("paged-end");
  if (!phone) {
    for (let i = 0; i < rounds; i += 1) {
      await returnToCard(page);
      await openInTab(page);
      mounts += 2;
    }
    frame = await viewDocument(page);
    await heroShot(page, shot, "hero-maximized");
    mounts += 1;
  }
  const end = await pageState(await viewDocument(page));
  const frames = (await appFrames(page)).map(({ element, ...row }) => row);
  return { session, start, states, end, mounts, frames };
}
