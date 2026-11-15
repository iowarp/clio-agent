// PL-Q5: a plain request in an existing conversation (PL_SESSION "<workspace>/<session>"),
// answered as a person would wait for it. Prints the tool calls (load_skill shows a skill
// the agent chose to read) and the end of the answer.
import { api, send, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const [workspace, session] = (process.env.PL_SESSION || "").split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(5000);
  const log = [];
  await send(page, process.env.PL_ASK);
  log.push(`settled: ${await waitIdle(page, session, log, 10)}`);
  await page.waitForTimeout(3000);
  await shot("answered");
  const listed = await api("GET", `/v1/sessions/${session}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  // Newest first: the reply to this request is the first assistant message.
  const last = [...rows].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    .find((m) => m.role === "assistant");
  const parts = last?.parts || [];
  const calls = parts.filter((p) => p.type === "tool_call").map((p) => p.tool_name);
  const answer = parts.filter((p) => p.type === "text").map((p) => p.text).join("\n");
  // The plugin_change chips the transcript shows (PL-D1: a connector is never "connector-<id>").
  const chips = await page
    .locator('[data-slot="harness-injection"][data-source="plugin_change"] button')
    .evaluateAll((items) => items.map((item) => item.innerText.trim()));
  console.log(JSON.stringify({ calls, chips, answer: answer.slice(-1500), log }));
}
