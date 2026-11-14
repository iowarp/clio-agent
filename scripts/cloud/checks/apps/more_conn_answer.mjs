// More connectors (ledger "More connectors (MC, stack N=30)"): continue a conversation whose
// turn waits on the person, answering as a person would at any width.
//
//   MC_SESSION  "<workspace id>/<session id>"
//   MC_CHOICE   the label (regular expression) of the option to pick on a question card
//               (default: the first option)
//   MC_ADD=1    press Add on a connector or plugin offer card when one appears (every one)
//   MC_SEND     a plain request typed into the composer first (optional)
// It loops until the turn is idle: pick and send on a question card, Add on an offer card,
// allow pending permissions for the session; then shoots the answer and any app view.
import { allowPending, api, appFrames, send, showLastApp, toolCalls } from "./_lib.mjs";

export default async function (page, shot) {
  const [workspace, session] = process.env.MC_SESSION.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  const log = [];
  if (process.env.MC_SEND) {
    await send(page, process.env.MC_SEND);
    await page.waitForTimeout(5000);
  }
  // Several waiting cards are paged: answer the one on screen.
  const question = page
    .locator('[data-slot="ask-card"]:not([data-interaction-kind="connector_setup"]):not([data-interaction-kind="permission"])')
    .filter({ visible: true })
    .first();
  const offer = page
    .locator('[data-interaction-kind="connector_setup"]:is([data-intent="enable"], [data-intent="add_plugin"])')
    .filter({ visible: true })
    .first();
  const until = Date.now() + 12 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    await allowPending(session, log);
    if (await question.isVisible().catch(() => false)) {
      await question.scrollIntoViewIfNeeded().catch(() => undefined);
      await shot("question-card");
      const choice = process.env.MC_CHOICE ? new RegExp(process.env.MC_CHOICE) : null;
      const option = choice ? question.getByText(choice).first() : question.getByRole("radio").first();
      await option.click();
      await question.getByRole("button", { name: /Send response/ }).click();
      log.push("question: answered");
      await page.waitForTimeout(3000);
      continue;
    }
    if (process.env.MC_ADD === "1" && (await offer.isVisible().catch(() => false))) {
      await offer.scrollIntoViewIfNeeded().catch(() => undefined);
      await shot("offer-card");
      if (!(await offer.getByRole("button", { name: /^1?\s*Add$/ }).click({ timeout: 5000 }).then(() => true, () => false))) continue;
      log.push("offer: Add");
      await page.waitForTimeout(3000);
      continue;
    }
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    quiet = ["idle", "completed"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(4000);
  await shot("answered");
  const frames = await showLastApp(page, await appFrames(page));
  if (frames.length) await shot("view");
  const agent = await api("GET", `/v1/sessions/${session}/agent`);
  const here = agent.resolution.plugins.filter((p) => p.provenance?.source === "user").map((p) => p.id);
  const calls = (await toolCalls(session)).filter((c) => !/_(read_pdf_bytes|poll_pdf_commands|submit_\w+):/.test(c));
  console.log(JSON.stringify({ session, calls, frames, here, log }, null, 2));
}
