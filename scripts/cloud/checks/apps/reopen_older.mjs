// Q-O2 (MA5): reopening an OLDER card keeps one live inline App. Needs the reopen fixture
// saved as the connection `boards` (tests/fixtures/app_reopen_server.py). A new
// conversation asks for two boards; the first card is then older than the latest and its
// view closed. Reopen it: the view replays into its tab, so the conversation still has
// exactly one inline App frame (the latest) and the reopened view is in a tab.
// Prints one JSON line with the counts before and after.
import { openConversation, send, waitIdle } from "./_lib.mjs";

async function frames(page) {
  return {
    inline: await page.locator('[data-mcp-app-placement="inline"] [data-mcp-app-iframe]').count(),
    tab: await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').count(),
  };
}

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, "final-qo2");
  await send(page, process.env.QO2_PROMPT || "Open a board titled Alpha with 2 cards, then a second board titled Beta with 3 cards.");
  await waitIdle(page, session.id, log, 10);
  await page.waitForTimeout(4000);
  for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(2000);
  const before = await frames(page);
  await shot("two-boards");
  const line = page.locator("[data-mcp-app-reopen]:visible").first();
  await line.waitFor({ timeout: 60000 });
  await line.scrollIntoViewIfNeeded();
  await line.getByRole("button", { name: "Reopen" }).click();
  const allow = page.getByRole("button", { name: /Allow once/ });
  if (await allow.first().waitFor({ timeout: 20000 }).then(() => true, () => false)) await allow.first().click();
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 90000 });
  await page.waitForTimeout(6000);
  const after = await frames(page);
  await shot("older-reopened-in-tab");
  console.error(JSON.stringify({ session: session.id, before, after, log }));
}
