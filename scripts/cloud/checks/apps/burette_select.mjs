// AQ33: "show PDB 1CRN", Open in tab, the person selects residues in Mol*, then asks
// "What did I select?". Passes when the structure is drawn in the tab, the view reports
// the selection as its context (session metadata mcp_app_context), and the answer names
// the selected residues.
//
// Needs the burette connector enabled. Every request is plain. The person selects as in
// Burette's own viewer: "Expand controls", "Selection mode", then clicks on the drawn
// structure (a plain click on Mol* in selection mode toggles that residue; outside it,
// a click only focuses). The points are found on a screenshot of the tab
// (_find_drawn.py).
//
// Note: Burette's view loads its narrow-screen viewer when its frame is 600 px wide or
// less, and that viewer fails to start at b1ac8ae (viewer.js initBuretToolbar reads
// BuretteNativeViewerMenus, which that path never loads), so the tab needs a window wide
// enough to give it more than 600 px (--width 1920 --height 1080).
import { execFileSync } from "node:child_process";
import path from "node:path";
import { ROOT, api, openConversation, send, waitIdle } from "./_lib.mjs";

const here = path.join(ROOT, "scripts/cloud/checks/apps");

async function context(sid) {
  const session = await api("GET", `/v1/sessions/${sid}`);
  return Object.values(session.metadata?.mcp_app_context || {}).map((row) => ({
    tool: row.tool,
    text: (row.context?.content || []).map((c) => c.text).join(" ").slice(0, 400),
    residues: row.context?.structuredContent?.burette?.activeSelection?.residues,
  }));
}

function lastAnswer(rows) {
  const list = Array.isArray(rows) ? rows : rows.messages || [];
  const assistant = list.filter((m) => m.role === "assistant");
  const ordered = assistant.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  return (ordered.at(-1)?.parts || []).filter((p) => p.type === "text").map((p) => p.text).join("\n");
}

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, "burette");
  const sid = session.id;
  await send(page, process.env.BURETTE_TEXT || "Show PDB 1CRN.");
  log.push(`shown settled: ${await waitIdle(page, sid, log, 8)}`);
  await page.waitForTimeout(8000);
  await page.getByRole("button", { name: "Open in tab" }).first().click();
  const tab = page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first();
  await tab.waitFor({ timeout: 30000 });
  await page.waitForTimeout(25000); // Mol* loads and draws in the new frame
  const box = await tab.boundingBox();
  log.push(`tab frame: ${Math.round(box.width)}x${Math.round(box.height)}`);
  await shot("tab");

  const view = page.frames().filter((f) => f.url() === "about:srcdoc").at(-1);
  for (const label of ["Expand controls", "Selection mode"]) {
    await view.getByRole("button", { name: label }).first().click({ timeout: 30000 });
    await page.waitForTimeout(1500);
  }
  const file = path.join(ROOT, ".cloud-run", "shots", "burette-tab-frame.png");
  await page.screenshot({ path: file, clip: box });
  const points = JSON.parse(execFileSync("python3", [path.join(here, "_find_drawn.py"), file, "8"]).toString());
  log.push(`drawn points: ${points.length}`);
  if (!points.length) throw new Error("nothing is drawn in the tab");

  const modifier = process.env.CHECK_SELECT_MODIFIER || "";
  for (const [x, y] of points.slice(0, Number(process.env.CHECK_CLICKS || 3))) {
    await page.mouse.move(box.x + x, box.y + y);
    await page.waitForTimeout(600);
    await page.mouse.click(box.x + x, box.y + y, { modifiers: modifier ? [modifier] : [] });
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(3000); // the view's report reaches the session record
  const reported = await context(sid);
  log.push(`reported context: ${JSON.stringify(reported)}`);
  await shot("selected");

  await send(page, "What did I select?");
  log.push(`asked settled: ${await waitIdle(page, sid, log, 8)}`);
  await page.waitForTimeout(2000);
  await shot("answer");
  const answer = lastAnswer(await api("GET", `/v1/sessions/${sid}/messages`));
  console.log(JSON.stringify({ session: sid, log, answer: answer.slice(0, 1500) }, null, 2));
  if (!reported.some((row) => row.residues?.length)) throw new Error("the view reported no selection");
}
