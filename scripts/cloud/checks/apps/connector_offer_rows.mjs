// The "Offer connector" row of a finished conversation, expanded: the agent's reason,
// what the person chose, where it applies and the tools that joined (or "Not now").
//
//   OFFER_SESSION  the conversation (a session connector_offer.mjs left)
import { api } from "./_lib.mjs";

export default async function (page, shot) {
  const id = process.env.OFFER_SESSION;
  const session = await api("GET", `/v1/sessions/${id}`);
  await page.goto(new URL(`/workspaces/${session.workspace_id}/sessions/${id}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(5000);
  const group = page.getByText(/^Called \d+ tools?/).first();
  await group.scrollIntoViewIfNeeded();
  await group.click();
  await page.waitForTimeout(800);
  // The Load tool row names it too; the offer's own row comes after it.
  const row = page.getByText("Offer connector", { exact: true }).last();
  await row.scrollIntoViewIfNeeded();
  await row.click();
  await page.waitForTimeout(1500);
  await row.scrollIntoViewIfNeeded();
  await shot("offer-row");
  const text = await row.locator("xpath=ancestor::*[self::li or self::div][3]").innerText().catch(() => "");
  console.log(JSON.stringify({ session: id, row: text.slice(0, 1200) }, null, 2));
}
