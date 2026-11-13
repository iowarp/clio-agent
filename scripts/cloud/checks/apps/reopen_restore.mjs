// AQ25 and AQ26 (MA18, MA19): run after `--send` asked for two boards from the reopen
// fixture (tests/fixtures/app_reopen_server.py, registered as the connection `boards`), so
// the first board's card is older than the latest App and its view is closed.
//
// AQ25: reopen the first card (its saved call is replayed and the view mounts in the card),
// move it to a tab, close the tab (an older App's view closes with its tab, MA7), then reopen
// it from its card: the saved call runs again with the saved arguments and the view mounts in
// a tab, the saved display mode.
// AQ26: with that tab open and another tab in front, restart the whole stack (`stack.sh
// restart`) while the replay is set to fail (REOPEN_FAIL_FILE), open the session again: the
// tab is restored and replays only when first focused; the failure shows its reason and the
// saved call; after the fail file is removed, Retry mounts the view in the tab.
//
// Every replay goes through the session's permission rules: the step answers each ask with
// "Allow once". REOPEN_CALL_LOG is the fixture's call log, printed after each step.
import { execSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { agentUrl, check, expectText, log, sessionIdFrom } from "./_app_view.mjs";

const callLog = process.env.REOPEN_CALL_LOG;
const failFile = process.env.REOPEN_FAIL_FILE;

function calls() {
  if (!callLog || !existsSync(callLog)) return [];
  return readFileSync(callLog, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function allowOnce(page, timeout = 90000) {
  const button = page.getByRole("button", { name: /Allow once/ });
  await button.waitFor({ timeout });
  await button.click();
}

/** The board view inside the frame at `placement` (inline or tab). */
function board(page, placement) {
  return page
    .frameLocator(`[data-mcp-app-placement="${placement}"] [data-mcp-app-iframe]`)
    .first()
    .frameLocator("iframe");
}

async function boardText(page, placement) {
  const view = board(page, placement);
  await expectText(view.locator("#call"), /Produced by call \d+/, 90000);
  return {
    title: await view.locator("#title").textContent(),
    args: await view.locator("#args").textContent(),
    call: await view.locator("#call").textContent(),
  };
}

async function waitHttp(url, timeout = 600000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const ok = await fetch(url).then((r) => r.ok).catch(() => false);
    if (ok) return;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  throw new Error(`${url} did not answer`);
}

export default async function (page, shot) {
  const sid = sessionIdFrom(page);
  const sessionUrl = page.url();
  log("session", sid);
  const lines = page.locator("[data-mcp-app-reopen]");
  await lines.first().waitFor({ timeout: 60000 });
  const first = await lines.first().getAttribute("data-mcp-app-reopen");
  log("first card", first);
  log("calls before", calls());
  await lines.first().scrollIntoViewIfNeeded();
  await shot("older-card-closed");

  // AQ25, part 1: reopen the older card; its saved mode is inline, so it mounts in the card.
  await lines.first().getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  const inline = await boardText(page, "inline");
  log("reopened inline", inline);
  log("calls after inline reopen", calls().at(-1));
  await page.locator('[data-mcp-app-placement="inline"]').first().scrollIntoViewIfNeeded();
  await shot("reopened-inline");

  // Move it to a tab, then close the tab: an App that is not the latest closes with its tab.
  const card = page.locator('[data-mcp-app-placement="inline"]').first();
  const reopenedId = await card.getAttribute("data-mcp-app").catch(() => null);
  await card.getByRole("button", { name: "Open in tab" }).click();
  await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
  await boardText(page, "tab");
  await shot("in-tab");
  const tabId = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab");
  log("tab app", { tabId, reopenedId });
  const chip = page.locator(`[data-tab-id="mcp-app:${tabId}"]`);
  await chip.hover();
  await chip.locator('[data-slot="frame-tab-close"]').click({ force: true });
  await page.locator("[data-mcp-app-tab]").first().waitFor({ state: "detached", timeout: 30000 });
  await lines.first().waitFor({ timeout: 30000 });
  await lines.first().scrollIntoViewIfNeeded();
  await shot("tab-closed");

  // AQ25, part 2: reopen from the card; the saved mode is now fullscreen, so it opens a tab.
  const before = calls().length;
  await lines.first().getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 90000 });
  const tabbed = await boardText(page, "tab");
  const replay = calls().slice(before);
  log("AQ25 replay", { view: tabbed, calls: replay });
  check(replay.length === 1 && replay[0].title && replay[0].count !== undefined, "one replay call");
  check((await page.locator("[data-mcp-app-in-tab]").count()) >= 1, "the card shows the in-tab row");
  await shot("reopened-in-tab");

  // AQ26: another tab in front, so the app tab is restored hidden.
  await page.locator('[data-tab-id]:not([data-tab-id^="mcp-app:"])').first().click().catch(() => {
    log("no other tab", "the app tab stays in front");
  });
  if (failFile) writeFileSync(failFile, "fail\n");
  log("restart", "stack.sh restart");
  execSync("bash scripts/cloud/stack.sh restart", { stdio: "ignore" });
  await waitHttp(`${agentUrl()}/v1/health`);
  await waitHttp(new URL("/", sessionUrl).toString());
  await page.waitForTimeout(5000);
  await page.goto(sessionUrl);
  const restored = page.locator('[data-tab-id^="mcp-app:"]').first();
  await restored.waitFor({ timeout: 120000 });
  await page.waitForTimeout(8000);
  const beforeFocus = calls().length;
  log("restored tab", { id: await restored.getAttribute("data-tab-id"), callsBeforeFocus: beforeFocus });
  await shot("restored-hidden");
  await restored.click();
  await allowOnce(page);
  const failure = page.locator("[data-reopen-reason]").first();
  await failure.waitFor({ timeout: 90000 });
  log("AQ26 failed replay", {
    reason: await failure.getAttribute("data-reopen-reason"),
    text: await failure.innerText(),
    calls: calls().slice(beforeFocus),
  });
  await shot("restored-replay-failed");

  if (failFile) rmSync(failFile, { force: true });
  await failure.getByRole("button", { name: "Retry" }).click();
  await allowOnce(page);
  const restoredView = await boardText(page, "tab");
  log("AQ26 retried", { view: restoredView, last: calls().at(-1) });
  await shot("restored-replayed");
}
