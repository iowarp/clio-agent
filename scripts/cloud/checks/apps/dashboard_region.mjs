// A region comment over a dashboard that holds an app view (CM8): the dashboard is
// opened from its card, the surface's "Capture labelled regions" marks a box over its
// app frame, the comment is attached, and the marks are sent with no other text.
// The overview the agent receives must show the view's pixels, not a blank frame.
//
//   DASH_SESSION  /workspaces/<ws>/sessions/<sid>
//   DASH_TITLE    the dashboard's title on its card in the conversation
//   DASH_COMMENT  the comment typed on the region (default "what is this?")
// Prints DASH_REGION with the answer and the tools the turn called.
import { allowPending, api, send } from "./_lib.mjs";

async function settle(page, sid, minutes = 15) {
  const until = Date.now() + minutes * 60_000;
  const log = [];
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sid, log);
    const session = await api("GET", `/v1/sessions/${sid}`);
    quiet = ["idle", "completed", "error"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return;
    await page.waitForTimeout(3000);
  }
}

export default async function (page, shot) {
  const sid = process.env.DASH_SESSION.split("/").at(-1);
  await page.goto(new URL(process.env.DASH_SESSION, page.url()).toString());
  await page.waitForTimeout(6000);
  await page.getByText(process.env.DASH_TITLE, { exact: true }).last().click();
  const app = page.locator("[data-mcp-app-iframe]").filter({ visible: true }).last();
  await app.waitFor({ timeout: 120000 });
  const toggle = page.locator('[data-slot="sidebar-trigger"]').filter({ visible: true }).first();
  if (await toggle.count()) await toggle.click().catch(() => undefined);
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.isVisible().catch(() => false)) await maximize.click();
  await page.waitForTimeout(25000); // the view starts and draws
  await shot("dashboard");
  // The whole dashboard's "Capture labelled regions" is in its viewer's file actions (each
  // chart's own toolbar has one for that chart alone).
  await page.getByRole("button", { name: "File actions" }).filter({ visible: true }).last().click();
  await page.getByRole("menuitem", { name: /Capture labelled regions/ }).click();
  await page.waitForTimeout(1500);
  const add = page.getByRole("button", { name: "Add region" }).filter({ visible: true });
  if (await add.count()) await add.first().click();
  await page.waitForTimeout(1500);
  // The view in the canvas's dashboard (the conversation's card may hold another frame).
  const inCanvas = page
    .getByRole("complementary", { name: "Workspace canvas" })
    .locator("[data-mcp-app-iframe]")
    .filter({ visible: true })
    .first();
  const box = await ((await inCanvas.count()) ? inCanvas : app).boundingBox();
  console.error(`[dash] view box: ${JSON.stringify(box)}`);
  await page.mouse.move(box.x + box.width * 0.15, box.y + box.height * 0.2);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(box.x + box.width * (0.15 + 0.07 * i), box.y + box.height * (0.2 + 0.05 * i));
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
  const editor = page.getByPlaceholder("What should the agent notice here?");
  await editor.waitFor({ timeout: 20000 });
  await editor.fill(process.env.DASH_COMMENT || "what is this?");
  await page.getByRole("button", { name: /^Done$/ }).click();
  await page.waitForTimeout(1000);
  await shot("region-marked");
  await page.getByRole("button", { name: /Add \d+ marks? to message/ }).click();
  await page.waitForTimeout(6000);
  const restore = page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).first();
  if (await restore.isVisible().catch(() => false)) await restore.click();
  await page.waitForTimeout(3000);
  await shot("region-attached");
  await send(page, "");
  await page.waitForTimeout(4000);
  await settle(page, sid);
  await page.waitForTimeout(2000);
  await shot("region-answer");
  const listed = await api("GET", `/v1/sessions/${sid}/messages`);
  const rows = [...(listed.messages || [])].reverse();
  const last = rows.map((m) => m.role).lastIndexOf("user");
  const turn = rows.slice(last + 1);
  console.log(
    `DASH_REGION ${JSON.stringify({
      calls: turn.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_call").map((p) => p.tool_name)),
      answer: turn.flatMap((m) => (m.parts || []).filter((p) => p.type === "text").map((p) => p.text)).join("\n").slice(0, 2500),
    })}`,
  );
}
