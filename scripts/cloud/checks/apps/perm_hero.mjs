// Hero shot for permissions following the mode (apps ledger, PM) at 1440x900: in `ask`, the
// board's view fills a widened workbench (navigation collapsed) while its own tool's
// question ("Set highlight") waits in the conversation; then the answer and the result.
// Needs the board fixture saved as "board" (scripts/cloud/checks/apps/perm_mode.sh).
//
//   PERM_WS=perm-hero node scripts/cloud/ui_check.mjs ... --steps .../perm_hero.mjs
import { mkdirSync } from "node:fs";
import path from "node:path";
import { ROOT, api, send } from "./_lib.mjs";

const WS = process.env.PERM_WS || "perm-hero";

async function pending(sid) {
  return (await api("GET", `/v1/permissions?session_id=${sid}&status=pending`)).permissions || [];
}

async function idle(page, sid, answer) {
  for (let i = 0; i < 120; i += 1) {
    for (const row of await pending(sid)) await api("POST", `/v1/permissions/${row.id}`, { action: answer });
    if (["idle", "completed"].includes((await api("GET", `/v1/sessions/${sid}`)).status)) return;
    await page.waitForTimeout(3000);
  }
}

export default async function (page, shot) {
  const root = path.join(ROOT, ".cloud-run", "apps-work", WS);
  mkdirSync(root, { recursive: true });
  const workspace = await api("POST", "/v1/workspaces", { name: WS, root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id, approval_mode: "ask" });
  const sid = session.id;
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(4000);
  await send(page, "Open the cooling loop readings board.");
  await page.waitForTimeout(4000);
  await idle(page, sid, "allow");
  if (await page.locator('[data-slot="sidebar"][data-state="expanded"]').count())
    await page.locator('[data-sidebar="trigger"]').first().click();
  await page.getByRole("button", { name: "Open in tab" }).last().click();
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 30000 });
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" }).first();
  const box = await handle.boundingBox().catch(() => null);
  if (box) {
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + box.width / 2, y);
    await page.mouse.down();
    await page.mouse.move(560, y, { steps: 12 });
    await page.mouse.up();
  }
  await page.waitForTimeout(1500);
  await send(page, "Highlight the pump B vibration on the board.");
  for (let i = 0; i < 60 && !(await pending(sid)).length; i += 1) await page.waitForTimeout(2000);
  const row = (await pending(sid))[0];
  await page.getByRole("button", { name: /^(This session|Allow for this session)$/ }).first().waitFor();
  await page.waitForTimeout(1500);
  await shot("hero-question-set-highlight-board-in-tab");
  await page.getByRole("button", { name: /^(This session|Allow for this session)$/ }).first().click();
  await idle(page, sid, "allow_session");
  await page.waitForTimeout(2500);
  await shot("hero-answered-board-highlighted");
  console.log(`PERM_HERO ${JSON.stringify({ session: sid, question: row?.summary, tool: row?.tool_call?.tool_name })}`);
}
