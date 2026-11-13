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

/** Answer the replay's permission ask; a session or workspace grant may already allow it. */
async function allowOnce(page, timeout = 20000) {
  const button = page.getByRole("button", { name: /Allow once/ });
  const asked = await button.first().waitFor({ timeout }).then(() => true, () => false);
  if (asked) await button.first().click();
  log("permission asked", asked);
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

/** REOPEN_SEND: ask for the boards first, answering each permission ask, until the turn ends. */
async function sendAnswering(page, text, timeout = 600000) {
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/sessions\//, { timeout: 60000 });
  const sid = sessionIdFrom(page);
  const deadline = Date.now() + timeout;
  await page.waitForTimeout(5000);
  while (Date.now() < deadline) {
    const allow = page.getByRole("button", { name: /Allow once/ });
    if (await allow.count()) await allow.first().click().catch(() => undefined);
    const state = await fetch(`${agentUrl()}/v1/sessions/${sid}`).then((r) => r.json());
    if (state.status === "idle") break;
    await page.waitForTimeout(2000);
  }
  await page.waitForTimeout(3000);
}

export default async function (page, shot) {
  if (process.env.REOPEN_SEND) await sendAnswering(page, process.env.REOPEN_SEND);
  if (process.env.REOPEN_SESSION) {
    const ws = process.env.REOPEN_WORKSPACE || "ws_default";
    await page.goto(new URL(`/workspaces/${ws}/sessions/${process.env.REOPEN_SESSION}`, page.url()).toString());
    await page.waitForTimeout(6000);
  }
  // The older board's card sits in the turn's collapsed activity group.
  for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(1000);
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

  // AQ25, part 1: reopen the older card. It opens where the view last was: in the card
  // (saved inline) or in a tab (saved fullscreen). From the card, move it to a tab.
  const start = calls().length;
  await lines.first().getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  const placed = page.locator('[data-mcp-app-placement="inline"], [data-mcp-app-tab]').first();
  await placed.waitFor({ timeout: 90000 });
  const where = (await page.locator("[data-mcp-app-tab]").count()) ? "tab" : "inline";
  const first_view = await boardText(page, where);
  log("reopened", { where, view: first_view, calls: calls().slice(start) });
  check(calls().length === start + 1, "the saved call ran once");
  await page.locator(where === "tab" ? "[data-mcp-app-tab]" : '[data-mcp-app-placement="inline"]')
    .first().scrollIntoViewIfNeeded();
  await shot(`reopened-${where}`);
  if (where === "inline") {
    await page.locator('[data-mcp-app-placement="inline"]').first()
      .getByRole("button", { name: "Open in tab" }).click();
    await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
    await boardText(page, "tab");
    await shot("in-tab");
  }

  // Close the tab: an App that is not the conversation's latest closes with its tab (MA7).
  const tabId = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab");
  log("tab app", tabId);
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
  // Leave the page while CLIO is down, so its polling does not count as failed requests.
  await page.waitForTimeout(1500); // the workbench saves its tabs on change
  await page.goto("about:blank");
  execSync("bash scripts/cloud/stack.sh restart", { stdio: "ignore" });
  await waitHttp(`${agentUrl()}/v1/health`);
  await waitHttp(`${agentUrl()}/v1/workspaces`); // 503 while task services start
  await waitHttp(new URL("/", sessionUrl).toString());
  await page.waitForTimeout(5000);
  await page.goto(sessionUrl);
  await page.waitForTimeout(6000);
  // The person opens the workspace canvas; its tabs come back from the browser's storage.
  const canvas = page.getByRole("button", { name: "Open workspace canvas" });
  if (await canvas.count()) await canvas.first().click();
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

  // MA2/MA5 for a reopened view: the latest card (closed by the restart) is reopened, moved
  // to a tab, and the tab closed. It returns to its card instead of closing, with no new call.
  const latestLine = page.locator("[data-mcp-app-reopen]:visible").last();
  await latestLine.scrollIntoViewIfNeeded();
  const latestCard = await latestLine.getAttribute("data-mcp-app-reopen");
  const beforeLatest = calls().length;
  await latestLine.getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  const inlineFrame = page.locator('[data-mcp-app-placement="inline"]').last();
  await inlineFrame.waitFor({ timeout: 90000 });
  const latestId = await inlineFrame.getAttribute("data-mcp-app");
  await inlineFrame.getByRole("button", { name: "Open in tab" }).click();
  await page.locator(`[data-mcp-app-tab="${latestId}"]`).waitFor({ timeout: 30000 });
  await shot("latest-in-tab");
  const latestChip = page.locator(`[data-tab-id="mcp-app:${latestId}"]`);
  await latestChip.hover();
  await latestChip.locator('[data-slot="frame-tab-close"]').click({ force: true });
  const back = page.locator(`[data-mcp-app-placement="inline"][data-mcp-app="${latestId}"]`);
  await back.waitFor({ timeout: 30000 });
  await back.scrollIntoViewIfNeeded();
  const backView = page
    .frameLocator(`[data-mcp-app-placement="inline"][data-mcp-app="${latestId}"] [data-mcp-app-iframe]`)
    .frameLocator("iframe");
  await expectText(backView.locator("#call"), /Produced by call \d+/, 60000);
  check(calls().length === beforeLatest + 1, "returning to the card made no new call");
  log("RO-D12 returned to its card", {
    card: latestCard,
    view: latestId,
    calls: calls().slice(beforeLatest),
    text: await backView.locator("#call").textContent(),
  });
  await shot("latest-returned-to-card");
}
