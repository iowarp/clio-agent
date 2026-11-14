// Friction polish (FP-D7): a dashboard's linked chart moves the live view, and the view's
// own change log calls that change the person's.
//
//   FP_SESSION  /workspaces/<w>/sessions/<s> holding the dashboard's card
//   FP_TITLE    the dashboard's title on its card
//   FP_POINT    "x,y": the chart point to click, in page pixels with the canvas maximized
// Prints FP_DASH with the newest scene changes the view reported (its `changes`, with
// who made each) before and after the click.
import { allowPending, api, heroShot } from "./_lib.mjs";

async function changes(sid) {
  const session = await api("GET", `/v1/sessions/${sid}`);
  const rows = session.metadata?.mcp_app_context || {};
  return Object.values(rows).flatMap((row) =>
    (row.context?.structuredContent?.changes || []).map((c) => `${c.by}: ${c.text}`),
  );
}

export default async function (page, shot) {
  const sid = process.env.FP_SESSION.split("/").at(-1);
  const out = {};
  await page.goto(new URL(process.env.FP_SESSION, page.url()).toString());
  await page.waitForTimeout(6000);
  await page.getByText(process.env.FP_TITLE, { exact: true }).last().click();
  const sidebar = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  if (await sidebar.count()) await sidebar.click().catch(() => undefined);
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.count()) await maximize.click().catch(() => undefined);
  await page.waitForTimeout(25000);
  out.frameLabels = await page
    .locator("[data-mcp-app] iframe")
    .evaluateAll((frames) => frames.map((f) => f.getAttribute("title")));
  out.before = await changes(sid);
  await shot("dashboard");
  const [x, y] = (process.env.FP_POINT || "770,244").split(",").map(Number);
  await page.mouse.click(x, y);
  // The channel's call asks the person once ("Change the timestep"): allowed for the session.
  out.allowed = [];
  for (let i = 0; i < 8; i += 1) {
    await page.waitForTimeout(2000);
    await allowPending(sid, out.allowed);
  }
  await page.waitForTimeout(8000);
  out.after = await changes(sid);
  await shot("after-chart-click");
  await heroShot(page, shot, "hero");
  console.log(`FP_DASH ${JSON.stringify(out)}`);
}
