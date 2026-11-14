// AQ6 (and AQ7 with CARD_ACTION=decline) through the UI: a connector whose sign-in was revoked. The agent's call waits,
// the setup card appears in the conversation, and after the sign-in the person presses
// Continue and the same call returns its result, with no new message from the person.
//
// Under per-conversation connectors the agent first offers Fixture Sign-in and the step
// presses Add, as the person would; the waiting call's setup card follows.
// Needs the fixture connectors (connector-fixtures.env.sh) with fixture-signin enabled,
// its address set to the OAuth fixture server and signed in once (connectors_api.sh does
// this). The request is plain: it names no tool.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { AGENT, ROOT, allowPending, api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  await api("DELETE", "/v1/mcp/configuration/fixture-signin/authorization");
  const { session } = await openConversation(page, "connector-card");
  await send(page, "What does the Fixture Sign-in test account say about its status?");
  // The connector is enabled but in no conversation (CS-D1): the agent offers it first,
  // and the person adds it; then its call waits on the setup card for the sign-in.
  const offer = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"][data-intent="enable"]:visible');
  const card = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"]:not([data-intent="enable"]):visible').last();
  const until = Date.now() + 300000;
  while (!(await card.isVisible())) {
    if (Date.now() > until) throw new Error("no setup card appeared");
    await allowPending(session.id, log); // a person allowing the session's tool calls
    const add = offer.getByRole("button", { name: /^1?\s*Add$/ });
    if ((await offer.count()) && (await add.isEnabled().catch(() => false))) {
      await shot("offer");
      await add.click();
      log.push("offer: Add");
    }
    await page.waitForTimeout(2000);
  }
  await card.scrollIntoViewIfNeeded();
  await shot("card-waiting");
  const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
  log.push(`while the card waits, the turn is ${status}`);

  if (process.env.CARD_ACTION === "decline") {
    // AQ7: the person declines; the waiting call returns the typed refusal.
    await card.getByRole("button", { name: /Decline/ }).click();
    log.push(`settled: ${await waitIdle(page, session.id, log, 6)}`);
    await page.waitForTimeout(2000);
    await shot("declined");
    const rows = (await api("GET", `/v1/sessions/${session.id}/messages`)).messages || [];
    const results = rows.flatMap((m) =>
      (m.parts || [])
        .filter((p) => p.type === "tool_result" && p.tool_name === "fixture-signin_fixture_status")
        .map((p) => (p.content || []).map((c) => c.text || "").join(" ").slice(0, 300)),
    );
    console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), results, log }, null, 2));
    return;
  }
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
