// PL-Q4, PL-Q5: a conversation adds Home Assistant from its "Add plugin" picker, where
// it is listed once, as a plugin with what it brings; the plugin_change note names its
// skills; then a plain request about the home is answered with its skill and tools.
//
//   PL_FIRST    the first (plain) message, so the conversation has history
//   PL_ASK      the plain request after the plugin is added
//   PL_PLUGIN   the picker row to add (default "Home Assistant")
// Prints one JSON line: the picker's groups, the note, the tool calls and the answer.
import { api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => text.replace(/\s+/gu, " ").trim();

async function openAgentUse(page) {
  const open = page.getByRole("button", { name: "Open workspace canvas" });
  if (await open.count()) {
    await open.first().click();
    await settle(page, 2500);
  }
  const lists = page.getByRole("toolbar", { name: "Workbench lists" });
  await lists.waitFor({ state: "visible", timeout: 15000 }).catch(() => undefined);
  await lists.getByRole("button", { name: /^Agent/u }).first().click();
  await settle(page, 2000);
  const tools = page.locator('[data-agent-use="tools"]');
  if (await tools.count()) await tools.first().click();
  await settle(page, 2500);
}

export default async function (page, shot) {
  const log = [];
  const plugin = process.env.PL_PLUGIN || "Home Assistant";
  const { session } = await openConversation(page, process.env.PL_WORKSPACE || "pl-ha");
  await send(page, process.env.PL_FIRST || "Hello. In one sentence, what can you help me with here?");
  log.push(`first settled: ${await waitIdle(page, session.id, log, 5)}`);

  await openAgentUse(page);
  await page.getByRole("button", { name: /Add (package|plugin)/u }).first().click();
  await settle(page, 2000);
  const groups = await page.locator('[role="group"][cmdk-group], [cmdk-group]').evaluateAll((items) =>
    items.map((item) => item.innerText.replace(/\s+/gu, " ").trim().slice(0, 400)),
  );
  await shot("add-plugin-picker");
  const plugins = page.getByRole("group", { name: "Plugins" });
  await plugins.getByRole("option", { name: new RegExp(plugin, "u") }).first().click();
  await settle(page, 1500);
  const ask = page.getByText("Do you want to proceed?");
  await ask.waitFor({ timeout: 5000 }).catch(() => undefined);
  if (await ask.count()) {
    await shot("apply-mode");
    await page.getByRole("button", { name: /^Yes, as an injection/u }).first().click();
  }
  await settle(page, 6000);
  await shot("added");
  const tree = words(await page.locator('[data-agent-use], [aria-label="Removed plugins"]').first().innerText().catch(() => ""));

  await send(
    page,
    process.env.PL_ASK ||
      "What is the temperature at home right now, and how has the outside temperature changed over the last day?",
  );
  log.push(`ask settled: ${await waitIdle(page, session.id, log, 10)}`);
  await settle(page, 3000);
  await shot("answered");

  const listed = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  const notes = rows.flatMap((m) =>
    (m.parts || m.blocks || [])
      .filter((p) => p.type === "injection" || p.source === "plugin_change")
      .map((p) => String(p.text || "").slice(0, 600)),
  );
  const answer = rows
    .filter((m) => m.role === "assistant")
    .flatMap((m) => (m.parts || []).filter((p) => p.type === "text").map((p) => p.text))
    .join("\n")
    .slice(-1200);
  console.log(
    JSON.stringify({ session: session.id, groups, tree: tree.slice(0, 300), notes, calls: await toolCalls(session.id), answer, log }),
  );
}
