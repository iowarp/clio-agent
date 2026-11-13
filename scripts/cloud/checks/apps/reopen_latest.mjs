// RO-D12 (MA2, MA5 for a reopened view): in a conversation whose latest App card is the
// reopen fixture's board (REOPEN_SESSION, REOPEN_WORKSPACE), close that App on the server so
// its card offers Reopen, reopen it (it mounts in the card), move it to a tab, then close the
// tab: the view returns to its card instead of closing, with no new tool call. REOPEN_CALL_LOG
// is the fixture's call log. Each permission ask is answered with "Allow once".
import { existsSync, readFileSync } from "node:fs";
import { agentUrl, check, expectText, log } from "./_app_view.mjs";

const callCount = () =>
  process.env.REOPEN_CALL_LOG && existsSync(process.env.REOPEN_CALL_LOG)
    ? readFileSync(process.env.REOPEN_CALL_LOG, "utf8").trim().split("\n").filter(Boolean).length
    : 0;

async function allowOnce(page) {
  const button = page.getByRole("button", { name: /Allow once/ });
  if (await button.first().waitFor({ timeout: 20000 }).then(() => true, () => false))
    await button.first().click();
}

export default async function (page, shot) {
  const ws = process.env.REOPEN_WORKSPACE || "ws_default";
  const sid = process.env.REOPEN_SESSION;
  // The latest App part of the conversation, closed on the server as if its view had ended.
  const messages = await fetch(`${agentUrl()}/v1/sessions/${sid}/messages`).then((r) => r.json());
  const rows = Array.isArray(messages) ? messages : messages.messages ?? messages.items ?? [];
  const parts = rows.flatMap((m) => m.parts ?? []).filter((p) => p.type === "mcp_app");
  const latest = parts.at(-1);
  const closed = await fetch(
    `${agentUrl()}/v1/sessions/${sid}/mcp-apps/${latest.app_instance_id}?data_ref=${encodeURIComponent(latest.data_ref)}`,
    { method: "DELETE" },
  );
  log("closed the latest App", { card: latest.app_instance_id, status: closed.status });

  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  const line = page.locator(`[data-mcp-app-reopen="${latest.app_instance_id}"]:visible`);
  await line.waitFor({ timeout: 90000 });
  await line.scrollIntoViewIfNeeded();
  await shot("latest-closed");

  const before = callCount();
  await line.getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  const card = page.locator('[data-mcp-app-placement="inline"]').last();
  await card.waitFor({ timeout: 90000 });
  const viewId = await card.getAttribute("data-mcp-app");
  const inCard = page
    .frameLocator(`[data-mcp-app-placement="inline"][data-mcp-app="${viewId}"] [data-mcp-app-iframe]`)
    .frameLocator("iframe");
  await expectText(inCard.locator("#call"), /Produced by call \d+/, 90000);
  check(callCount() === before + 1, "the reopen ran the saved call once");
  await card.scrollIntoViewIfNeeded();
  await shot("reopened-in-card");

  await card.getByRole("button", { name: "Open in tab" }).click();
  await page.locator(`[data-mcp-app-tab="${viewId}"]`).waitFor({ timeout: 30000 });
  check(
    (await page.getByRole("button", { name: "Return to conversation" }).count()) === 1,
    "the tab offers Return to conversation (the view is its card's latest App)",
  );
  await shot("reopened-in-tab");

  const chip = page.locator(`[data-tab-id="mcp-app:${viewId}"]`);
  await chip.hover();
  await chip.locator('[data-slot="frame-tab-close"]').click({ force: true });
  const back = page.locator(`[data-mcp-app-placement="inline"][data-mcp-app="${viewId}"]`);
  await back.waitFor({ timeout: 30000 });
  await expectText(inCard.locator("#call"), /Produced by call \d+/, 60000);
  check(callCount() === before + 1, "returning to the card made no new call");
  check(
    (await page.locator(`[data-mcp-app-reopen="${latest.app_instance_id}"]:visible`).count()) === 0,
    "the card shows the view, not Reopen",
  );
  const src = await page.locator(`[data-mcp-app="${viewId}"] [data-mcp-app-iframe]`).getAttribute("src");
  const dataRef = src ? new URL(src).searchParams.get("data_ref") ?? "" : "";
  const live = await fetch(
    `${agentUrl()}/v1/sessions/${sid}/mcp-apps/${viewId}/live?data_ref=${encodeURIComponent(dataRef)}`,
  ).then((r) => r.json());
  log("RO-D12", { card: latest.app_instance_id, view: viewId, calls: callCount() - before, live });
  await back.scrollIntoViewIfNeeded();
  await shot("returned-to-card");
}
