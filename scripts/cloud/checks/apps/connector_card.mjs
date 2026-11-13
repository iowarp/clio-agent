// AQ6 through the UI: a connector whose sign-in was revoked. The agent's call waits,
// the setup card appears in the conversation, and after the sign-in the person presses
// Continue and the same call returns its result, with no new message from the person.
//
// Needs the fixture connectors (connector-fixtures.env.sh) with fixture-signin enabled,
// its address set to the OAuth fixture server and signed in once (connectors_api.sh does
// this). The request is plain: it names no tool.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { AGENT, ROOT, api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  await api("DELETE", "/v1/mcp/configuration/fixture-signin/authorization");
  const { session } = await openConversation(page, "connector-card");
  await send(page, "What does the synthetic test account say about its status?");
  const card = page.locator('[data-interaction-kind="connector_setup"]').last();
  await card.waitFor({ state: "visible", timeout: 300000 });
  await card.scrollIntoViewIfNeeded();
  await shot("card-waiting");
  const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
  log.push(`while the card waits, the turn is ${status}`);

  // The person signs in (the fixture's consent page, driven like the browser would).
  const ui = new URL(page.url()).origin;
  execFileSync(
    "uv",
    ["run", "--no-sync", "python", "-B", path.join(ROOT, "scripts/cloud/checks/apps/connector_sign_in.py"),
      "--agent", AGENT, "--ui", ui, "--connector", "fixture-signin"],
    { stdio: "inherit", cwd: ROOT },
  );
  const proceed = card.getByRole("button", { name: /Continue/ });
  await page.waitForFunction((el) => el && !el.disabled, await proceed.elementHandle(), { timeout: 60000 });
  await shot("card-signed-in");
  await proceed.click();
  log.push(`settled: ${await waitIdle(page, session.id, log, 6)}`);
  await shot("answered");
  const messages = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = messages.messages || [];
  const people = rows.filter((m) => m.role === "user").length;
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), people, log }, null, 2));
  if (people !== 1) throw new Error(`expected one message from the person, found ${people}`);
}
