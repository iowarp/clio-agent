// CBO2 through the UI: the server revokes the sign-in on its side while CLIO still
// holds it. The agent's call is allowed, the server refuses it, and the call waits on
// the same setup card; after the person signs in again and presses Continue, the same
// call returns its result, with no new message from the person.
//
// Needs the fixture connectors (connector-fixtures.env.sh), fixture-signin enabled with
// its address set to the OAuth fixture server (tests/fixtures/oauth_mcp_server.py on
// CONNECTOR_FIXTURE_PORT, default 19101). The request is plain: it names no tool.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { AGENT, ROOT, allowPending, api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const FIXTURE = `http://127.0.0.1:${process.env.CONNECTOR_FIXTURE_PORT || 19101}`;

function signIn(ui) {
  execFileSync(
    "uv",
    ["run", "--no-sync", "python", "-B", path.join(ROOT, "scripts/cloud/checks/apps/connector_sign_in.py"),
      "--agent", AGENT, "--ui", ui, "--connector", "fixture-signin"],
    { stdio: "inherit", cwd: ROOT },
  );
}

export default async function (page, shot) {
  const log = [];
  const ui = new URL(page.url()).origin;
  signIn(ui); // CLIO holds a sign-in the server accepts
  const before = await api("GET", "/v1/connectors/fixture-signin");
  log.push(`before: ${before.state.status}`);
  const revoked = await (await fetch(`${FIXTURE}/fixture-revoke`, { method: "POST" })).json();
  log.push(`revoked on the server: ${revoked.revoked} credentials`);
  const held = await api("GET", "/v1/connectors/fixture-signin");
  log.push(`CLIO still reads: ${held.state.status}`);

  const { session } = await openConversation(page, "connector-server-revoke");
  await send(page, "What does the Fixture Sign-in test account say about its status?");
  const card = page.locator('[data-interaction-kind="connector_setup"]').last();
  const until = Date.now() + 300000;
  while (!(await card.isVisible())) {
    if (Date.now() > until) throw new Error("no setup card appeared");
    await allowPending(session.id, log);
    await page.waitForTimeout(2000);
  }
  await card.scrollIntoViewIfNeeded();
  await shot("card-after-server-refused");
  log.push(`while the card waits, the turn is ${(await api("GET", `/v1/sessions/${session.id}`)).status}`);

  signIn(ui);
  const proceed = card.getByRole("button", { name: /Continue/ });
  await page.waitForFunction((el) => el && !el.disabled, await proceed.elementHandle(), { timeout: 60000 });
  await proceed.click();
  log.push(`settled: ${await waitIdle(page, session.id, log, 6)}`);
  await shot("answered");
  const rows = (await api("GET", `/v1/sessions/${session.id}/messages`)).messages || [];
  const people = rows.filter((m) => m.role === "user").length;
  const results = rows.flatMap((m) => m.parts || []).filter((p) => p.type === "tool_result");
  const text = results.map((p) => (p.content || []).map((c) => c.text || "").join(" ")).join(" | ");
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), people, results: text, log }, null, 2));
  if (people !== 1) throw new Error(`expected one message from the person, found ${people}`);
  if (!text.includes("Synthetic qualification account")) throw new Error("the call did not return the account");
}
