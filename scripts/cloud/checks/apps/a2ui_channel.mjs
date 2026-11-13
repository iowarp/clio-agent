// clio.mcp-app.v1 in a surface beside a chart bound to the same /selection/time path
// (plan AQ27, AQ28, AQ29). Open the conversation that holds the surface
// (CHECK_SESSION_PATH, e.g. /sessions/sess_x), then:
//   AQ27  move the view's slider to step 5: the chart's selection follows, and the
//         channel calls no tool (nothing is sent back to the view);
//   AQ28  choose time 9 from the chart's own "Select a time by keyboard" menu: the
//         channel calls set_time once with 9 and the view shows 9;
//   AQ29  press the view's burst button (sixty reports in one second): at most ten
//         data-model writes, the last value wins.
// Channel counters come from the component's data-* attributes; the fixture server
// logs each set_time call to CHANNEL_APP_LOG, which the caller counts.
const result = {};

function channel(page) {
  return page.locator('[data-mcp-app-channel="time"]').first();
}

async function counters(page) {
  const row = channel(page);
  return {
    reports: Number((await row.getAttribute("data-reports")) ?? 0),
    writes: Number((await row.getAttribute("data-writes")) ?? 0),
    toolCalls: Number((await row.getAttribute("data-tool-calls")) ?? 0),
    lastWritten: await row.getAttribute("data-last-written"),
  };
}

// The opening call and the channel's tool follow the session's tool permission rules:
// when the conversation asks, allow the call once.
async function allowIfAsked(page, label, until) {
  const allow = page.getByRole("button", { name: /Allow once/ }).first();
  const deadline = Date.now() + 90000;
  let shown = false;
  while (Date.now() < deadline) {
    if (await allow.isVisible().catch(() => false)) {
      if (!shown) await shot_(label);
      shown = true;
      await allow.scrollIntoViewIfNeeded().catch(() => {});
      await allow.click({ timeout: 10000 }).catch(async (error) => {
        console.log(`A2UI_CHANNEL_CLICK ${String(error).slice(0, 1500)}`);
        await allow.press("Enter");
      });
      result.permissions = [...(result.permissions ?? []), label];
      await page.waitForTimeout(1500);
      continue;
    }
    if (await until().catch(() => false)) return;
    await page.waitForTimeout(500);
  }
}
let shot_ = async () => {};

function view(page) {
  // The host frame, then the sandbox's inner document that holds the view.
  return page.frameLocator("[data-mcp-app-iframe]").first().frameLocator("iframe").first();
}

export default async function (page, shot) {
  shot_ = shot;
  const path = process.env.CHECK_SESSION_PATH;
  if (path) {
    await page.goto(new URL(path, page.url()).toString());
    await page.waitForTimeout(3000);
  }
  const connected = view(page).locator("#status").filter({ hasText: "Connected" });
  await allowIfAsked(page, "permission-open", () => connected.isVisible());
  const app = page.locator("[data-mcp-app-component]").first();
  await app.scrollIntoViewIfNeeded({ timeout: 60000 });
  const step = view(page).locator("#step");
  await view(page).locator("#status").filter({ hasText: "Connected" }).waitFor({ timeout: 60000 });
  await page.waitForTimeout(1000);
  await shot("surface-open");
  result.opened = await counters(page);

  // AQ27: change time in the view.
  await view(page)
    .locator("#slider")
    .evaluate((input) => {
      input.value = "5";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  await page.waitForTimeout(1500);
  result.aq27 = {
    ...(await counters(page)),
    viewStep: await step.textContent(),
    hostSets: await view(page).locator("#counts").getAttribute("data-host-sets"),
  };
  await shot("aq27-view-moved-chart-follows");

  // AQ28: change time from the chart's own selection menu.
  const chart = page.locator('[data-slot="a2ui-chart"]').first();
  await chart.scrollIntoViewIfNeeded();
  await chart.hover();
  const more = chart.getByRole("button", { name: "More" }).first();
  await more.click();
  await page.getByRole("menuitem", { name: /Select a time by keyboard/i }).first().hover();
  await page.getByRole("menuitemradio", { name: /^9$/ }).first().click();
  await page.keyboard.press("Escape").catch(() => {});
  const hostSet = view(page).locator("#counts[data-host-sets='1']");
  await allowIfAsked(page, "permission-set-time", () => hostSet.isVisible());
  await view(page).locator("#counts[data-host-sets='1']").waitFor({ timeout: 20000 });
  await page.waitForTimeout(1000);
  result.aq28 = {
    ...(await counters(page)),
    viewStep: await step.textContent(),
    hostSets: await view(page).locator("#counts").getAttribute("data-host-sets"),
  };
  await shot("aq28-chart-drives-view");

  // AQ29: sixty reports in one second.
  const before = await counters(page);
  await view(page).locator("#burst").click();
  await view(page).locator("#status").filter({ hasText: "Burst done" }).waitFor({ timeout: 20000 });
  await page.waitForTimeout(500);
  const after = await counters(page);
  result.aq29 = {
    reports: after.reports - before.reports,
    writes: after.writes - before.writes,
    toolCalls: after.toolCalls - before.toolCalls,
    lastWritten: after.lastWritten,
    viewStatus: await view(page).locator("#status").textContent(),
  };
  await shot("aq29-burst");
  console.log(`A2UI_CHANNEL_RESULT ${JSON.stringify(result)}`);
}
