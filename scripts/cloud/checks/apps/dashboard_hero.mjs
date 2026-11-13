// The ParaView dashboard (an A2UI surface with clio.mcp-app.v1 and clio.chart.v1 bound to
// one `time` path), shown as the person sees it: opened from its card in the conversation,
// navigation collapsed and the canvas maximized (desktop), so the view and the chart are in
// one frame. Shots in the dark and the light theme, before and after the chart is scrubbed.
//
//   DASH_SESSION  /workspaces/<ws>/sessions/<sid>
//   DASH_TITLE    the dashboard's title on its card in the conversation
//   DASH_SEND     optional plain follow-up request sent first (waits for the turn)
//   DASH_TIME     the time chosen from the chart's keyboard menu (default 0.3)
//   DASH_REMOTE=1 press the view's "Render on ParaView machine" first (drawing toggle)
//   DASH_SHOTS_ONLY=1  stop after the opening shots (no scrub)
// Prints DASH_RESULT with the channel counters and the view's time bar before and after.
import { allowPending, send, waitIdle } from "./_lib.mjs";

const result = { log: [] };
const phone = () => page_.viewportSize().width < 600;
let page_;

async function counters(page) {
  const row = page.locator('[data-mcp-app-channel="time"]').filter({ visible: true }).first();
  if (!(await row.count())) return null;
  return {
    reports: Number((await row.getAttribute("data-reports")) ?? 0),
    writes: Number((await row.getAttribute("data-writes")) ?? 0),
    toolCalls: Number((await row.getAttribute("data-tool-calls")) ?? 0),
    lastWritten: await row.getAttribute("data-last-written"),
  };
}

/** The time bar of every ParaView frame that is drawn (the dashboard's, and any tab's). */
async function bars(page) {
  const out = [];
  for (const frame of page.frames().filter((f) => f.url().includes("/service/"))) {
    const text = await frame.locator(".clio-bar").innerText({ timeout: 2000 }).catch(() => "");
    if (text) out.push(text.replace(/\s+/g, " ").trim());
  }
  return out;
}

async function themed(page, shot, label) {
  for (const theme of ["dark", "light"]) {
    await page.evaluate((dark) => {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      document.documentElement.classList.toggle("dark", dark);
    }, theme === "dark");
    await page.waitForTimeout(5000);
    await shot(`${label}-${theme}`);
  }
}

export default async function (page, shot) {
  page_ = page;
  page.on("pageerror", (error) =>
    result.log.push({ pageerror: error?.message, stack: String(error?.stack || "").slice(0, 600) }),
  );
  const sid = process.env.DASH_SESSION.split("/").at(-1);
  await page.goto(new URL(process.env.DASH_SESSION, page.url()).toString());
  await page.waitForTimeout(6000);
  if (process.env.DASH_SEND) {
    await send(page, process.env.DASH_SEND);
    await page.waitForTimeout(4000);
    await waitIdle(page, sid, result.log, 20);
  }
  // Open the dashboard from its card (the last one with its title).
  await page.getByText(process.env.DASH_TITLE, { exact: true }).last().click();
  await page.locator("[data-mcp-app-component]").filter({ visible: true }).first().waitFor({ timeout: 120000 });
  if (!phone()) {
    const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
    if (navOpen) {
      const toggle = page.locator('[data-slot="sidebar-trigger"]').filter({ visible: true }).first();
      if (await toggle.count()) await toggle.click().catch(() => undefined);
    }
    const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
    if (await maximize.isVisible().catch(() => false)) await maximize.click();
  }
  await page.waitForTimeout(20000); // the view starts and draws
  if (process.env.DASH_REMOTE === "1") {
    for (const frame of page.frames().filter((f) => f.url().includes("/service/"))) {
      const button = frame.getByRole("button", { name: "Render on ParaView machine" });
      if (await button.count()) await button.first().click().catch(() => undefined);
    }
    await page.waitForTimeout(8000);
  }
  result.opened = { counters: await counters(page), bars: await bars(page) };
  await themed(page, shot, "open");
  if (process.env.DASH_SHOTS_ONLY === "1") {
    console.log(`DASH_RESULT ${JSON.stringify(result)}`);
    return;
  }

  // Scrub the chart: the view's timestep follows through the channel's tool.
  const chart = page.locator('[data-slot="a2ui-chart"]').filter({ visible: true }).first();
  await chart.scrollIntoViewIfNeeded();
  await chart.hover();
  await chart.getByRole("button", { name: "More" }).first().click();
  await page.getByRole("menuitem", { name: /Select a time by keyboard/i }).first().hover();
  const time = process.env.DASH_TIME || "0.3";
  await page.getByRole("menuitemradio", { name: new RegExp(`^${time.replace(".", "\\.")}$`) }).first().click();
  await page.keyboard.press("Escape").catch(() => {});
  // The channel's call goes through the session's permission rules, as any call does: a
  // person allows it (here for the session) and the view follows.
  const want = `${Math.round(Number(time) * 10) + 1}/12`;
  for (let i = 0; i < 40; i += 1) {
    await allowPending(sid, result.log);
    if ((await bars(page)).some((bar) => bar.includes(want))) break;
    await page.waitForTimeout(1500);
  }
  if (!phone()) {
    const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
    if (await maximize.isVisible().catch(() => false)) await maximize.click();
  }
  await page.waitForTimeout(6000);
  await page.locator("[data-mcp-app-component]").filter({ visible: true }).first().scrollIntoViewIfNeeded();
  result.scrubbed = { time, counters: await counters(page), bars: await bars(page) };
  await themed(page, shot, "scrubbed");
  console.log(`DASH_RESULT ${JSON.stringify(result)}`);
}
