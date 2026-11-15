// AQ34: with a structure in a tab, "Focus the ligand and hide water." The agent acts
// through the view's app tools or render_molecular_scene, and the view changes without
// reloading.
//
// Needs the burette connector enabled. 1CRN has neither ligand nor water, so the step
// opens a structure that has both (BURETTE_PDB, default 3PTB: trypsin with benzamidine
// and waters). Requests are plain. "Without reloading" is measured, not assumed: every
// navigation of a view frame after the request is counted, with where its frame sits
// (card or tab), and the tool calls and app parts of the turn are listed.
// Run wide enough that the tab is over 600 px (see burette_select.mjs).
import { api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const where = async (frame) => {
  const element = await frame.parentFrame()?.frameElement().catch(() => null);
  if (!element) return "?";
  return element.evaluate((el) => el.closest("[data-mcp-app-placement]")?.getAttribute("data-mcp-app-placement") || "?");
};

export default async function (page, shot) {
  const log = [];
  const pdb = process.env.BURETTE_PDB || "3PTB";
  const { session } = await openConversation(page, "burette");
  const sid = session.id;
  await send(page, `Show PDB ${pdb}.`);
  log.push(`shown settled: ${await waitIdle(page, sid, log, 8)}`);
  await page.waitForTimeout(8000);
  await page.getByRole("button", { name: "Open in tab" }).first().click();
  const tab = page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first();
  await tab.waitFor({ timeout: 30000 });
  await page.waitForTimeout(25000);
  const tabApp = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab");
  log.push(`tab app: ${tabApp}`);
  await shot("before");

  const navigations = [];
  const t0 = Date.now();
  page.on("framenavigated", async (frame) => {
    if (frame.url() !== "about:srcdoc" && !/\/mcp-apps\//.test(frame.url())) return;
    navigations.push({ at: Math.round((Date.now() - t0) / 1000), url: frame.url().slice(0, 60), in: await where(frame) });
  });
  await send(page, process.env.BURETTE_SCENE_TEXT || "Focus the ligand and hide water.");
  log.push(`scene settled: ${await waitIdle(page, sid, log, 8)}`);
  await page.waitForTimeout(15000);
  await shot("after");
  const context = Object.entries((await api("GET", `/v1/sessions/${sid}`)).metadata?.mcp_app_context || {}).map(
    ([app, row]) => ({ app, tool: row.tool, text: row.context?.content?.[0]?.text, scene: row.context?.structuredContent?.burette?.scene?.results }),
  );
  // The scene's own card, opened in a tab by the person, to see what it drew.
  const scene = page.locator("[data-mcp-app-placement=inline]").last();
  await scene.scrollIntoViewIfNeeded().catch(() => {});
  await page.getByRole("button", { name: "Open in tab" }).last().click().catch((e) => log.push(`no open in tab: ${e.message.slice(0, 80)}`));
  await page.waitForTimeout(25000);
  await shot("scene-in-tab");

  const { messages } = await api("GET", `/v1/sessions/${sid}/messages`);
  const apps = (messages || [])
    .flatMap((m) => m.parts || [])
    .filter((p) => p.type === "mcp_app")
    .map((p) => ({ app: p.app_instance_id, tool: p.tool_name, placement: p.placement, mode: p.display_mode }));
  const tabNow = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab").catch(() => null);
  console.log(
    JSON.stringify(
      { session: sid, calls: await toolCalls(sid), apps, context, tabApp, tabNow, navigations, frames: await page.locator("[data-mcp-app-iframe]").count(), log },
      null,
      2,
    ),
  );
}
