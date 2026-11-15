// Live check (fix stream): a view whose server stopped is not live; a dead service says so.
//
// Needs scripts/cloud/checks/apps/stopped_server.sh, which saves the service fixture
// (tests/fixtures/mcp_app_service_server.py) as "counter", opens the counter view in a
// conversation and exports STOPPED_WORKSPACE and STOPPED_SESSION. STOPPED_PHASE picks
// the phase:
//   open      the conversation with the counter card; the card is put in its tab
//             ("Ready"); the page then leaves, so nobody views the conversation.
//   reopen    after the reaper closed the fleet: the card offers Reopen (not "Show
//             tab"); Reopen replays the opening call and the counter mounts again.
//   down      the service process was stopped while the view's server stayed: the tab
//             reads "Service unavailable" with the reason, not "Ready", and the
//             service's own address shows a readable page instead of JSON.
async function allowOnce(page) {
  const button = page.getByRole("button", { name: /Allow once/ });
  if (await button.first().waitFor({ timeout: 15000 }).then(() => true, () => false))
    await button.first().click();
}

async function openSession(page) {
  const ws = process.env.STOPPED_WORKSPACE;
  const sid = process.env.STOPPED_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(6000);
  for (const group of await page.getByRole("button", { name: /Called \d+ tools?|used tools/i }).all())
    await group.click().catch(() => undefined);
}

export default async function (page, shot) {
  const phase = process.env.STOPPED_PHASE || "open";
  const report = { phase };
  await openSession(page);
  if (phase === "open") {
    const frame = page.locator("[data-mcp-app-iframe]:visible").last();
    await frame.waitFor({ timeout: 90000 });
    await frame.scrollIntoViewIfNeeded();
    await page.waitForTimeout(3000);
    await shot("counter-card-live");
    const toTab = page.getByRole("button", { name: /Open in tab/i }).last();
    if (await toTab.isVisible().catch(() => false)) {
      await toTab.click();
      await page.waitForTimeout(4000);
      await shot("counter-in-tab");
    }
    report.header = await page.locator("[data-mcp-app] [role=status]").last().innerText().catch(() => "");
  } else if (phase === "reopen") {
    const line = page.locator("[data-mcp-app-reopen]:visible").last();
    await line.waitFor({ timeout: 60000 });
    await line.scrollIntoViewIfNeeded();
    report.card = await line.innerText();
    report.show_tab_offered = await page.getByRole("button", { name: /Show tab/i }).isVisible().catch(() => false);
    await shot("card-offers-reopen-after-reap");
    await line.getByRole("button", { name: "Reopen" }).click();
    await allowOnce(page);
    const frame = page.locator("[data-mcp-app-iframe]:visible").last();
    await frame.waitFor({ timeout: 90000 });
    await frame.scrollIntoViewIfNeeded();
    await page.waitForTimeout(5000);
    await shot("reopened-counter-live");
    report.header = await page.locator("[data-mcp-app] [role=status]").last().innerText().catch(() => "");
    if (process.env.STOPPED_KILL === "1") {
      // The service's process goes away while the view's record stays live.
      const { execSync } = await import("node:child_process");
      // Only this stack's own fixture process (its path is in this worktree).
      // "[.]py" keeps the pattern from matching this shell's own command line.
      execSync(`pkill -f "${process.env.CLAUDE_PROJECT_DIR}/tests/fixtures/mcp_app_service_server[.]py" || true`);
      await page.waitForTimeout(2000);
      const src = await frame.getAttribute("src").catch(() => null);
      // A fresh mount reads the descriptor again: the view goes back to its card.
      const back = page.getByRole("button", { name: /Return to conversation/i }).last();
      if (await back.isVisible().catch(() => false)) await back.click();
      await page.waitForTimeout(5000);
      const app = page.locator("[data-mcp-app]:visible").last();
      await app.waitFor({ timeout: 60000 });
      await app.scrollIntoViewIfNeeded();
      const refusal = page.locator("[data-mcp-app-service-refusal]:visible").last();
      await refusal.waitFor({ timeout: 60000 });
      report.down_refusal = await refusal.innerText();
      report.down_header = await app.locator("[role=status]").first().innerText();
      await shot("service-down-tab-says-so");
      if (src) {
        const url = new URL(src);
        const [, , , sid, , appId] = url.pathname.split("/");
        const ref = url.searchParams.get("data_ref");
        await page.goto(`${url.origin}/v1/sessions/${sid}/mcp-apps/${appId}/service/${ref}/`);
        await page.waitForTimeout(1500);
        report.service_page = (await page.locator("body").innerText()).slice(0, 300);
        await shot("service-down-readable-page");
      }
    }
  } else {
    const app = page.locator("[data-mcp-app]:visible").last();
    await app.waitFor({ timeout: 90000 });
    await app.scrollIntoViewIfNeeded();
    const refusal = page.locator("[data-mcp-app-service-refusal]:visible").last();
    await refusal.waitFor({ timeout: 60000 });
    report.refusal = await refusal.innerText();
    report.header = await app.locator("[role=status]").first().innerText();
    await shot("service-down-tab-says-so");
    const url = process.env.STOPPED_SERVICE_URL;
    if (url) {
      await page.goto(url);
      await page.waitForTimeout(1500);
      report.service_page = (await page.locator("body").innerText()).slice(0, 300);
      await shot("service-down-readable-page");
    }
  }
  console.log(JSON.stringify(report, null, 2));
  return report;
}
