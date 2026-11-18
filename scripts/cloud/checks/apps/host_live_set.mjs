// AQ15: with the host panel in a tab (after host_tab.mjs, same session), the agent
// opens a second app inline, then a third. The tab app stays live and answers a
// server tool call; only the latest inline app is live.
// Set CHECK_SESSION_PATH to the session's path when the run starts on another page.
import {
  check,
  expectText,
  json,
  log,
  sendAndSettle,
  sessionIdFrom,
  viewIn,
} from "./_app_view.mjs";
import { allowPending } from "./_lib.mjs";

export async function appsOf(sid) {
  const { body } = await json(`/v1/sessions/${sid}/messages?limit=200`);
  const newestFirst = body?.messages ?? body?.items ?? body ?? [];
  const blocks = [];
  for (const message of [...newestFirst].reverse()) {
    for (const block of message.blocks ?? message.parts ?? []) {
      if (block.type === "mcp_app") blocks.push(block);
    }
  }
  return blocks;
}

export async function alive(sid, block, page) {
  // The call goes through the session's permission rules: allow it as a person would.
  let done = false;
  const allowing = (async () => {
    while (!done) {
      await allowPending(sid, []).catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  })();
  const { status } = await json(
    `/v1/sessions/${sid}/mcp-apps/${block.app_instance_id}/tools/call?data_ref=${encodeURIComponent(block.data_ref)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Referer: page.url() },
      body: JSON.stringify({ name: "ping", arguments: { note: "probe" } }),
    },
  ).finally(() => {
    done = true;
  });
  await allowing;
  return status;
}

export default async function (page, shot) {
  if (process.env.CHECK_SESSION_PATH) {
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  const sid = sessionIdFrom(page);
  log("session", sid);
  const tab = viewIn(page, "tab");
  await expectText(tab.locator("#mode"), /^fullscreen$/, 60000);

  await sendAndSettle(page, "Open the host panel again, for the topic beta.");
  await expectText(viewIn(page, "inline").locator("#topic"), /beta/, 60000);
  await shot("second-inline");
  await sendAndSettle(page, "Now open it once more, for the topic gamma.");
  await expectText(viewIn(page, "inline").locator("#topic"), /gamma/, 60000);

  const blocks = await appsOf(sid);
  log("apps in transcript", blocks.map((b) => [b.app_instance_id, b.display_mode]));
  check(blocks.length >= 3, "three apps were opened");
  const [first, second, third] = blocks.slice(-3);
  const statuses = {
    tab: await alive(sid, first, page),
    second: await alive(sid, second, page),
    third: await alive(sid, third, page),
  };
  log("tools/call status by app", statuses);
  check(statuses.tab === 200, "the tab app answers a tool call");
  check(statuses.second === 404, "the replaced inline app is closed");
  check(statuses.third === 200, "the latest inline app is live");

  // The tab app's own view still calls its server.
  await page.getByRole("tab", { name: /Open Panel|open_panel/ }).first().click();
  await tab.locator("#ping").click();
  const pong = await expectText(tab.locator("#status"), /server tool: pong|refused/);
  log("tab view ping", pong);
  check(/pong/.test(pong), "the tab view's tool call succeeded");
  check((await page.locator("[data-mcp-app-iframe]").count()) === 2, "two frames: tab and latest card");
  await shot("tab-live-after-third");
}
