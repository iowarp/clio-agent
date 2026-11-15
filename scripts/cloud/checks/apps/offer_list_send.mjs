// A new conversation and one plain request (ledger OL2 to OL4): OL_REQUEST is typed into
// the composer as a person would; nothing in it names a tool or a plugin. Offer cards
// are left waiting (shot, never answered) unless OL_ANSWER is "add" or "not-now".
// Prints OFFER_LIST_SEND {session, path, calls, offered, log}.
import { allowPending, api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  const { session, workspace } = await openConversation(page, process.env.OL_NAME || "offer-list");
  await send(page, process.env.OL_REQUEST || "Hello! In two sentences, what can you help me with?");
  const card = page.locator('[data-interaction-kind="connector_setup"]').last();
  const until = Date.now() + 6 * 60_000;
  let offered = false;
  while (Date.now() < until) {
    await allowPending(session.id, log);
    if (await card.isVisible().catch(() => false)) {
      offered = true;
      break;
    }
    const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
    if (["idle", "completed"].includes(status) && Date.now() > until - 5 * 60_000) {
      await page.waitForTimeout(4000);
      if (["idle", "completed"].includes((await api("GET", `/v1/sessions/${session.id}`)).status)) break;
    }
    await page.waitForTimeout(2500);
  }
  if (offered) {
    await card.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1500);
    log.push(`card: ${(await card.innerText()).replace(/\s+/g, " ").slice(0, 400)}`);
    await shot("offer-card");
    const answer = process.env.OL_ANSWER;
    if (answer) {
      await card.getByRole("button", { name: answer === "add" ? /^Add/ : "Not now" }).first().click();
      log.push(`settled: ${await waitIdle(page, session.id, log, 6)}`);
    }
  } else {
    log.push(`settled: ${await waitIdle(page, session.id, log, 2)}`);
  }
  await page.waitForTimeout(2000);
  await shot("conversation");
  const path = `/workspaces/${workspace.id}/sessions/${session.id}`;
  console.log(
    `OFFER_LIST_SEND ${JSON.stringify({ session: session.id, path, calls: await toolCalls(session.id), offered, log })}`,
  );
}
