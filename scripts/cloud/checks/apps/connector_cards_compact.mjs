// The compact connector cards (CF-D5): the offer card of a connector with a required
// need, then the setup card of the same connector once its secret is gone.
//
// Needs the fixture connectors (connector-fixtures.env.sh). The first request is plain;
// the second, after the token is cleared through the API, asks for the same check again,
// so its call waits on the setup card (CN5). Shots: the offer card (required row, quiet
// Settings), the setup card, then the answered turn.
import { allowPending, api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const TOKEN = "cf-fixture-token";

async function waitCard(page, selector, sessionId, log) {
  const card = page.locator(selector).last();
  const until = Date.now() + 6 * 60_000;
  while (!(await card.isVisible())) {
    if (Date.now() > until) throw new Error(`no card ${selector}`);
    await allowPending(sessionId, log);
    await page.waitForTimeout(2000);
  }
  await card.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1200);
  return card;
}

export default async function (page, shot) {
  const log = [];
  await api("POST", "/v1/connectors/fixture-secret/disable").catch(() => undefined);
  await api("PUT", "/v1/connectors/fixture-secret/values", { values: { token: null } }).catch(() => undefined);
  const { session } = await openConversation(page, "cf-cards");
  await send(page, "What does the Fixture Secret test server report about its status?");
  const offer = await waitCard(page, '[data-interaction-kind="connector_setup"][data-intent="enable"]', session.id, log);
  await shot("offer-card");
  const settings = offer.getByRole("button", { name: /^Settings \(/ });
  if (await settings.count()) {
    await settings.click();
    await page.waitForTimeout(600);
    await shot("offer-card-settings-open");
    await settings.click();
  }
  await offer.getByLabel(/Access token/).fill(TOKEN);
  await offer.getByRole("button", { name: /^1?\s*Add$/ }).click();
  log.push(`offer answered; settled: ${await waitIdle(page, session.id, log, 8)}`);
  await api("PUT", "/v1/connectors/fixture-secret/values", { values: { token: null } });
  await send(page, "Check its status once more, please.");
  const setup = await waitCard(
    page,
    '[data-interaction-kind="connector_setup"]:not([data-intent="enable"]):not([data-intent="add_plugin"])',
    session.id,
    log,
  );
  await shot("setup-card");
  await setup.getByLabel(/Access token/).fill(TOKEN);
  await setup.getByRole("button", { name: /Continue/ }).click();
  log.push(`setup answered; settled: ${await waitIdle(page, session.id, log, 8)}`);
  await page.waitForTimeout(2000);
  await shot("answered");
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), log }, null, 2));
}
