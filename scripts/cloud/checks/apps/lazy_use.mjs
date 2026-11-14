// LZ phase 2: a connector enabled from Discover, used from a plain request in a new
// conversation of the default agent. When the agent offers it (the offer card), the step
// chooses Add as a person would, and records that it did. Prints one JSON line.
//
//   LZ_WORKSPACE  workspace folder name (default "lazy-h")
//   LZ_SEND       the plain request
//   LZ_ADD        a plugin ref to add to the conversation first, as the Add plugin picker
//                 does (PATCH .../agent with the delta), e.g. clio/hdf5
import { allowPending, api, openConversation, send, toolCalls } from "./_lib.mjs";

const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

export default async function (page, shot) {
  const report = { offers: [] };
  const log = [];
  const { session } = await openConversation(page, process.env.LZ_WORKSPACE || "lazy-h");
  report.session = session.id;
  report.agentCard = await page.locator('[data-intent="agent_needs"]').count();
  if (process.env.LZ_ADD) {
    const added = await api("PATCH", `/v1/sessions/${session.id}/agent`, {
      add: [process.env.LZ_ADD],
      apply: "note",
    });
    report.added = (added.resolution?.plugins || []).map((row) => row.id).filter((id) => id === process.env.LZ_ADD.split("/").at(-1));
    report.servers = Object.keys(added.resolution?.mcp_servers || {});
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page, 4000);
  }
  await send(page, process.env.LZ_SEND || "What is in run.h5?");
  const until = Date.now() + 12 * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(session.id, log);
    const offer = page.locator('[data-interaction-kind="connector_setup"][data-intent="enable"], [data-interaction-kind="connector_setup"][data-intent="add_plugin"]').first();
    if (await offer.isVisible().catch(() => false)) {
      report.offers.push(words(await offer.innerText()).slice(0, 400));
      await shot("offer");
      await offer.getByRole("button", { name: /^1?\s*Add/u }).first().click();
      await settle(page, 2500);
    }
    const state = await api("GET", `/v1/sessions/${session.id}`);
    quiet = ["idle", "completed"].includes(state.status) ? quiet + 1 : 0;
    if (quiet >= 3) break;
    await page.waitForTimeout(3000);
  }
  await settle(page, 2500);
  report.tools = await toolCalls(session.id);
  await shot("answer");
  const messages = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = Array.isArray(messages) ? messages : messages.messages || [];
  const last = rows.filter((m) => m.role === "assistant").at(-1);
  report.answer = words((last?.parts || []).filter((p) => p.type === "text").map((p) => p.text).join(" ")).slice(0, 700);
  report.log = log;
  console.log(JSON.stringify(report));
}
