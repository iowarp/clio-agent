// Hero shots for the test grounds (AQ32, AQ33, AQ34) at 1440x900: the left navigation
// collapsed and the workbench maximized, so the view fills most of the frame.
//
// CHECK_HERO picks the scenario, every request plain:
//   spice     "Simulate … with ngspice." then "Show me V(out)."; the chart's card opens in
//             a tab
//   select    "Show PDB 1CRN."; in the tab the person turns on selection mode and clicks
//             three points of the drawing, then asks "What did I select?"
//   scene     "Show PDB 3PTB.", then "Focus the ligand and hide water."; the scene's card
//             opens in a tab
// The molecular view's tab is opened at a width over 600 px (openWideTab): Burette's
// narrow-screen viewer fails to start upstream (ledger q-grounds QG-B1).
import { copyFileSync, mkdirSync } from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { ROOT, api, openConversation, send, waitIdle } from "./_lib.mjs";

const here = path.join(ROOT, "scripts/cloud/checks/apps");

async function collapseSidebar(page) {
  // The sidebar's state persists between visits: toggle it only while it is expanded.
  if (!(await page.locator('[data-state="expanded"][data-side="left"], [data-slot="sidebar"][data-state="expanded"]').count())) return;
  const trigger = page.locator('[data-sidebar="trigger"]').first();
  if (await trigger.count()) await trigger.click();
  await page.waitForTimeout(1200);
}

async function openLastInTab(page) {
  await page.getByRole("button", { name: "Open in tab" }).last().click();
  const frame = page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first();
  await frame.waitFor({ timeout: 30000 });
  return frame;
}

/** Move the divider between conversation and canvas to `targetX` (the layout persists). */
async function widenCanvas(page, targetX) {
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" }).first();
  const box = await handle.boundingBox().catch(() => null);
  console.error(`[check] canvas divider: ${box ? Math.round(box.x) : "not found"}`);
  if (!box) return;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move((x + targetX) / 2, y, { steps: 8 });
  await page.mouse.move(targetX, y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(1500);
}

/**
 * Open the latest view in a tab whose frame is wider than 600 px when it loads: open it,
 * widen the canvas, return it to its card, and open it again (a tab's frame is sized
 * when it mounts; Burette picks its viewer by that width).
 */
async function openWideTab(page, log) {
  let frame = await openLastInTab(page);
  log.push(`first tab frame width: ${Math.round((await frame.boundingBox()).width)}`);
  console.error(`[check] ${log.at(-1)}`);
  await widenCanvas(page, 600);
  await page.getByRole("button", { name: "Return to conversation" }).first().click();
  await page.waitForTimeout(3000);
  frame = await openLastInTab(page);
  log.push(`tab frame width at load: ${Math.round((await frame.boundingBox()).width)}`);
  console.error(`[check] ${log.at(-1)}`);
  return frame;
}

async function maximize(page) {
  await page.getByRole("button", { name: "Maximize canvas" }).first().click({ timeout: 15000 });
  // The canvas remounts in its maximized host, which reloads an app's frame: wait for it.
  await page.waitForTimeout(25000);
}

async function ask(page, sid, text, log) {
  await send(page, text);
  log.push(`${text} settled: ${await waitIdle(page, sid, log, 8)}`);
}

export default async function (page, shot) {
  const log = [];
  const hero = process.env.CHECK_HERO || "spice";
  await collapseSidebar(page);
  const { root, session } = await openConversation(page, hero === "spice" ? "spice" : "burette");
  const sid = session.id;
  await collapseSidebar(page);
  if (hero === "spice") {
    copyFileSync(path.join(ROOT, "tests/fixtures/apps/rc_lowpass.cir"), path.join(root, "rc_lowpass.cir"));
    await ask(page, sid, `Simulate the circuit in ${path.join(root, "rc_lowpass.cir")} with ngspice.`, log);
    await ask(page, sid, "Show me V(out).", log);
    await page.waitForTimeout(6000);
    await shot("inline");
    const frame = await openLastInTab(page);
    log.push(`tab frame width at load: ${Math.round((await frame.boundingBox()).width)}`);
    await maximize(page);
    await page.waitForTimeout(6000);
    await shot("hero");
  } else if (hero === "select") {
    await ask(page, sid, "Show PDB 1CRN.", log);
    await page.waitForTimeout(6000);
    const frame = await openWideTab(page, log);
    await page.waitForTimeout(25000);
    await maximize(page);
    await page.waitForTimeout(6000);
    // The view is the frame inside the tab's sandbox frame.
    const sandbox = await (await frame.elementHandle()).contentFrame();
    // Burette's desktop shell nests the Mol* viewer one frame deeper: use the deepest.
    const inside = (f) => { for (let p = f; p; p = p.parentFrame()) if (p === sandbox) return true; return false; };
    let view = sandbox;
    for (let i = 0; i < 20 && view === sandbox; i++) {
      for (const f of page.frames().filter(inside)) {
        if (await f.locator('button[aria-label="Expand controls"], button[aria-label="Collapse controls"]').count().catch(() => 0)) view = f;
      }
      if (view === sandbox) await page.waitForTimeout(1500);
    }
    console.error(`[check] viewer frame found: ${view !== sandbox}`);
    const labels = async () =>
      view.evaluate(() => [...document.querySelectorAll("button")].filter((b) => b.getBoundingClientRect().width > 0).map((b) => b.getAttribute("aria-label") || b.textContent.trim().slice(0, 20)));
    console.error(`[check] view buttons: ${JSON.stringify(await labels())}`);
    const expand = view.getByRole("button", { name: "Expand controls" });
    if (await expand.count()) await expand.first().click({ timeout: 30000 });
    await page.waitForTimeout(1500);
    console.error(`[check] view buttons after expand: ${JSON.stringify(await labels())}`);
    await view.getByRole("button", { name: "Selection mode" }).first().click({ timeout: 30000 });
    await page.waitForTimeout(1500);
    const box = await frame.boundingBox();
    const file = path.join(ROOT, ".cloud-run", "shots", "hero-tab-frame.png");
    await page.screenshot({ path: file, clip: box });
    const points = JSON.parse(execFileSync("python3", [path.join(here, "_find_drawn.py"), file, "8"]).toString());
    for (const [x, y] of points.slice(0, 3)) {
      await page.mouse.move(box.x + x, box.y + y);
      await page.waitForTimeout(600);
      await page.mouse.click(box.x + x, box.y + y);
      await page.waitForTimeout(1500);
    }
    await page.waitForTimeout(3000);
    await shot("hero-selected");
    // The question goes in the conversation: restore the canvas beside it, ask, maximize again.
    await page.getByRole("button", { name: "Restore canvas beside conversation" }).first().click();
    await page.waitForTimeout(1500);
    await ask(page, sid, "What did I select?", log);
    await page.waitForTimeout(2000);
    await shot("answer");
  } else {
    await ask(page, sid, "Show PDB 3PTB.", log);
    await page.waitForTimeout(6000);
    await ask(page, sid, "Focus the ligand and hide water.", log);
    await page.waitForTimeout(15000);
    await shot("scene-card");
    await openWideTab(page, log);
    await page.waitForTimeout(25000);
    await maximize(page);
    await page.waitForTimeout(6000);
    await shot("hero");
  }
  const context = Object.values((await api("GET", `/v1/sessions/${sid}`)).metadata?.mcp_app_context || {}).map(
    (row) => row.context?.content?.[0]?.text,
  );
  mkdirSync(path.join(ROOT, ".cloud-run", "shots"), { recursive: true });
  console.log(JSON.stringify({ session: sid, hero, context, log }, null, 2));
}
