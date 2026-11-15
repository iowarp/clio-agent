// In QUANTUM_SESSION's live results view, click the point of parameter set QUANTUM_SET (as
// the person would), then send QUANTUM_TEXT and print the reported selection and the answer.
import { api, send, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const sid = process.env.QUANTUM_SESSION;
  const session = await api("GET", `/v1/sessions/${sid}`);
  await page.goto(new URL(`/workspaces/${session.workspace_id}/sessions/${sid}`, page.url()).toString(), { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(12000);
  const host = page.locator("[data-mcp-app-iframe]").last();
  await host.scrollIntoViewIfNeeded();
  const view = page.frameLocator("[data-mcp-app-iframe] >> nth=-1").frameLocator("iframe");
  const set = process.env.QUANTUM_SET || "6";
  const point = view.locator(`[aria-label^="set: ${set};"], [aria-label*="Parameter set: ${set};"]`).first();
  const found = await point.count();
  if (found) await point.click({ force: true });
  await page.waitForTimeout(2000);
  const selected = ((await view.locator(".selection").first().textContent().catch(() => "")) || "").trim();
  await shot("selected");
  const context = await api("GET", `/v1/sessions/${sid}`).then((s) => (s.metadata || {}).mcp_app_context || {});
  await send(page, process.env.QUANTUM_TEXT || "Why does this image sit so close to the boundary, and could noise flip it?");
  const log = [];
  log.push(`settled: ${await waitIdle(page, sid, log, 15)}`);
  await page.waitForTimeout(3000);
  await shot("answer");
  const rows = await api("GET", `/v1/sessions/${sid}/messages?limit=4`);
  const last = (rows.messages || rows).filter((m) => m.role === "assistant").at(-1);
  console.log(JSON.stringify({ found, selected, context: Object.values(context).map((c) => (c.context || {}).structuredContent?.selection),
    answer: (last?.parts || []).filter((p) => p.type === "text").map((p) => p.text).join("\n").slice(0, 2500),
    calls: (await toolCalls(sid)).slice(-12), log }, null, 2));
}
