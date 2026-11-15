// Plain request for a linked app-and-chart surface (evidence of agent judgement for
// clio.mcp-app.v1, beside the tool-directed AQ27-AQ29 run). Prints the surface's
// components as the agent produced them.
import { api, openConversation, send, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, "a2ui-channel-plain");
  await send(
    page,
    "Put the timeline view next to a chart of value = step squared / 10 for steps 0 to 23, linked so that picking a step in either one moves the other.",
  );
  await waitIdle(page, session.id, log);
  await page.waitForTimeout(4000);
  const app = page.locator("[data-mcp-app-component]").first();
  if (await app.count()) await app.scrollIntoViewIfNeeded().catch(() => {});
  await shot("plain-request");
  const surfaces = await api("GET", `/v1/sessions/${session.id}/a2ui/surfaces`).catch((e) => ({ error: String(e) }));
  console.log(`A2UI_CHANNEL_PLAIN ${JSON.stringify({ session: session.id, log, surfaces }).slice(0, 6000)}`);
}
