// Permissions follow the mode (apps ledger, PM): every kind of call a connector or a view
// makes asks in `ask` (once per tool after "This session" / "This workspace") and never
// asks in `bypass`. scripts/cloud/checks/apps/perm_mode.sh saves the fixtures and runs this.
//
// PERM_RUN picks the round; each opens a new conversation in workspace PERM_WS
// (.cloud-run/apps-work/<PERM_WS>) with approval mode PERM_MODE (default: ask, bypass
// for the bypass round). The requests are plain (they name no tool) except the A2UI
// surface, whose request is tool-directed (mechanism only).
//
//   bypass   every kind of call; no permission question may appear at all
//   ask-a    board open (Allow once), a second board (asked again: Allow once), the older
//            card's Reopen replay (This session), a third board (no question), highlight (This session), highlight again (no
//            question), host panel open + the view's own tools/call (This session; a
//            second click asks nothing), set_connector_value (This session), the A2UI
//            channel's open and set_time (This session)
//   ask-b    a new session asks again: board open, highlight, set_connector_value
//            (This workspace each)
//   ask-c    a third session in the workspace: board open, highlight, set_connector_value
//            ask nothing
//   phone    board open, highlight, set_connector_value, the view's tools/call at phone
//            width (This session), with a shot of each question
//
// Prints PERM_RESULT {...}: per request, the questions asked (title, tool) and the answer.
import { api, ROOT, send } from "./_lib.mjs";
import { mkdirSync } from "node:fs";
import path from "node:path";

const RUN = process.env.PERM_RUN || "ask-a";
const MODE = process.env.PERM_MODE || (RUN === "bypass" ? "bypass" : "ask");
const WS = process.env.PERM_WS || `perm-${RUN}`;
const NODE = process.env.PERM_NODE || "node08";
const result = { run: RUN, mode: MODE, steps: [] };

const BOARD = "Open the cooling loop readings board.";
const HEATING = "Open the readings board for the heating loop.";
const PUMPS = "Open the readings board for the pump loop.";
const HIGHLIGHT = (what) => `Highlight the ${what} temperature on the board.`;
const PANEL = "Open the host feature panel for the topic permissions.";
const WHERE = (node) => `The Fixture Secret connector now runs on host ${node}; set that.`;
const A2UI =
  'Use create_a2ui_surface to make surface timeline_dash. Its root is a Column with two children. First, a clio.mcp-app.v1 component with id app, server timeline, tool open_timeline, arguments {"steps": 24}, state [{"key": "time", "path": "/selection/time", "tool": "set_time"}] and height 360. Second, a clio.chart.v1 with id chart, title "Value by time step", preset scatter, xField time, yField value, entityField time, selection {"path": "/selection/time"}, selectionField time, and inline data rows for time 0 to 23 where value is time*time/10. Do nothing else and reply with one short sentence.';

const BUTTONS = {
  allow: /^Allow once$/,
  allow_session: /^(This session|Allow for this session)$/,
  allow_workspace: /^(This workspace|Allow for this workspace)$/,
};

async function conversation(page) {
  const root = path.join(ROOT, ".cloud-run", "apps-work", WS);
  mkdirSync(root, { recursive: true });
  const listed = await api("GET", "/v1/workspaces");
  const rows = Array.isArray(listed) ? listed : listed.workspaces || listed.items || [];
  const workspace =
    rows.find((w) => w.root_path === root) || (await api("POST", "/v1/workspaces", { name: WS, root_path: root }));
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id, approval_mode: MODE });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(4000);
  result.workspace = workspace.id;
  result.session = session.id;
  return session.id;
}

async function rows(sid, status = "all") {
  return (await api("GET", `/v1/permissions?session_id=${sid}&status=${status}&limit=500`)).permissions || [];
}

/** Questions are rows a person saw: everything but the auto-approved audit rows. */
const asked = (all) => all.filter((r) => r.status !== "auto_approved" && r.status !== "auto_denied");

/**
 * Answer every question that appears until `done()` holds, through the real card: shot
 * the card, then press the answer's button. `answers` maps a tool (or "*") to the answer.
 */
async function settle(page, shot, sid, step, answers, done, minutes = 6) {
  const until = Date.now() + minutes * 60000;
  let quiet = 0;
  while (Date.now() < until) {
    for (const row of await rows(sid, "pending")) {
      const tool = row.tool_call?.tool_name || "";
      const answer = answers[tool] || answers["*"] || "allow_session";
      step.asked.push({ title: row.summary, tool, answer });
      if (MODE === "bypass") throw new Error(`bypass asked: ${row.summary} (${tool})`);
      await page.getByRole("button", { name: BUTTONS[answer] }).first().waitFor({ timeout: 30000 });
      await page.waitForTimeout(800);
      await shot(`question-${step.name}-${step.asked.length}`);
      await page.getByRole("button", { name: BUTTONS[answer] }).first().click();
      await page.waitForTimeout(1500);
    }
    if (await done()) quiet += 1;
    else quiet = 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(2500);
  }
  return false;
}

async function idle(sid) {
  const session = await api("GET", `/v1/sessions/${sid}`);
  return ["idle", "completed"].includes(session.status);
}

/** One plain request; questions answered per `answers`; `expect` names how many asked. */
async function turn(page, shot, sid, name, text, answers = {}, expect = undefined) {
  const step = { name, text, asked: [] };
  await send(page, text);
  await page.waitForTimeout(4000);
  step.settled = await settle(page, shot, sid, step, answers, () => idle(sid));
  await shot(`after-${name}`);
  step.tools = await calls(sid);
  if (expect !== undefined && step.asked.length !== expect) step.unexpected = `asked ${step.asked.length}, expected ${expect}`;
  result.steps.push(step);
  return step;
}

async function calls(sid) {
  const listed = await api("GET", `/v1/sessions/${sid}/messages`);
  const last = (listed.messages || []).find((m) => m.role === "assistant"); // newest first
  return (last?.parts || [])
    .filter((p) => p.type === "tool_call" || p.type === "mcp_app")
    .map((p) => `${p.tool_name}:${p.status || p.state?.status || ""}`);
}

async function panelFrame(page) {
  for (let i = 0; i < 60; i += 1) {
    for (const frame of page.frames()) {
      if (!frame.parentFrame() || !/\/mcp-apps\/[^/]+\/sandbox/.test(frame.parentFrame().url())) continue;
      if (await frame.locator("#ping").count().catch(() => 0)) return frame;
    }
    await page.waitForTimeout(1000);
  }
  throw new Error("the host panel view did not load");
}

/** The view's own tools/call: press its button, answer the question, read the result. */
async function viewCall(page, shot, sid, name, answers, expect) {
  const step = { name, text: "view button: Call server tool", asked: [] };
  const frame = await panelFrame(page);
  await frame.locator("#ping").scrollIntoViewIfNeeded().catch(() => {});
  await frame.locator("#ping").click();
  step.settled = await settle(page, shot, sid, step, answers, async () =>
    /pong|server tool:|denied|not allowed/i.test((await frame.locator("#status").textContent()) || ""),
  );
  step.view = await frame.locator("#status").textContent();
  await shot(`after-${name}`);
  await frame.evaluate(() => (document.getElementById("status").textContent = "-"));
  if (expect !== undefined && step.asked.length !== expect) step.unexpected = `asked ${step.asked.length}, expected ${expect}`;
  result.steps.push(step);
}

/** The card's Reopen on an older board (the newest view stays open): replays its saved call. */
async function reopen(page, shot, sid, name, answers, expect) {
  const step = { name, text: "card: Reopen the older board", asked: [] };
  for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
    await group.click().catch(() => undefined);
  const line = page.locator("[data-mcp-app-reopen]:visible").first();
  await line.waitFor({ timeout: 60000 });
  await line.scrollIntoViewIfNeeded();
  const before = await page.locator("[data-mcp-app-iframe]").count();
  await line.getByRole("button", { name: "Reopen" }).click();
  step.settled = await settle(page, shot, sid, step, answers, async () =>
    (await page.locator("[data-mcp-app-reopen]:visible").count()) === 0 ||
    (await page.locator("[data-mcp-app-iframe]").count()) !== before,
  );
  await page.waitForTimeout(3000);
  await shot(`after-${name}`);
  step.replay = (await page.locator("[data-reopen-reason]:visible").count()) ? "failed" : "reopened";
  if (expect !== undefined && step.asked.length !== expect) step.unexpected = `asked ${step.asked.length}, expected ${expect}`;
  result.steps.push(step);
}

/** The A2UI component's opening call, then set_time from the chart's selection menu. */
async function a2ui(page, shot, sid, answers) {
  await turn(page, shot, sid, "a2ui-surface", A2UI, answers);
  const step = { name: "a2ui-set-time", text: "chart: select time 9", asked: [] };
  const chart = page.locator('[data-slot="a2ui-chart"]').first();
  await chart.scrollIntoViewIfNeeded();
  await chart.hover();
  await chart.getByRole("button", { name: "More" }).first().click();
  await page.getByRole("menuitem", { name: /Select a time by keyboard/i }).first().hover();
  await page.getByRole("menuitemradio", { name: /^9$/ }).first().click();
  await page.keyboard.press("Escape").catch(() => {});
  const row = page.locator('[data-mcp-app-channel="time"]').first();
  step.settled = await settle(page, shot, sid, step, answers, async () =>
    Number((await row.getAttribute("data-tool-calls")) ?? 0) >= 1,
  );
  step.channel = { toolCalls: await row.getAttribute("data-tool-calls"), last: await row.getAttribute("data-last-written") };
  await shot("after-a2ui-set-time");
  result.steps.push(step);
}

export default async function (page, shot) {
  const sid = await conversation(page);
  const session = "allow_session";
  const workspace = "allow_workspace";
  try {
    if (RUN === "bypass") {
      await turn(page, shot, sid, "board", BOARD, {}, 0);
      await turn(page, shot, sid, "highlight", HIGHLIGHT("outlet"), {}, 0);
      await turn(page, shot, sid, "board-again", HEATING, {}, 0);
      await reopen(page, shot, sid, "reopen", {}, 0);
      await turn(page, shot, sid, "panel", PANEL, {}, 0);
      await viewCall(page, shot, sid, "view-call", {}, 0);
      await turn(page, shot, sid, "connector-value", WHERE(NODE), {}, 0);
      await a2ui(page, shot, sid, {});
    } else if (RUN === "ask-a") {
      await turn(page, shot, sid, "board", BOARD, { "*": "allow" }, 1);
      await turn(page, shot, sid, "board-again", HEATING, { "*": "allow" }, 1); // once was once
      await reopen(page, shot, sid, "reopen", { "*": session }, 1);
      await turn(page, shot, sid, "board-third", PUMPS, {}, 0);
      await turn(page, shot, sid, "highlight", HIGHLIGHT("outlet"), { "*": session }, 1);
      await turn(page, shot, sid, "highlight-again", HIGHLIGHT("inlet"), {}, 0);
      await turn(page, shot, sid, "panel", PANEL, { "*": session }, 1);
      await viewCall(page, shot, sid, "view-call", { "*": session }, 1);
      await viewCall(page, shot, sid, "view-call-again", {}, 0);
      await turn(page, shot, sid, "connector-value", WHERE(NODE), { "*": session }, 1);
      await turn(page, shot, sid, "connector-value-again", WHERE(`${NODE}b`), {}, 0);
      await a2ui(page, shot, sid, { "*": session });
    } else if (RUN === "ask-b") {
      await turn(page, shot, sid, "board", BOARD, { "*": workspace }, 1);
      await turn(page, shot, sid, "highlight", HIGHLIGHT("outlet"), { "*": workspace }, 1);
      await turn(page, shot, sid, "connector-value", WHERE(NODE), { "*": workspace }, 1);
    } else if (RUN === "ask-c") {
      await turn(page, shot, sid, "board", BOARD, {}, 0);
      await turn(page, shot, sid, "highlight", HIGHLIGHT("outlet"), {}, 0);
      await turn(page, shot, sid, "connector-value", WHERE(NODE), {}, 0);
    } else if (RUN === "phone") {
      await turn(page, shot, sid, "board", BOARD, { "*": session }, 1);
      await turn(page, shot, sid, "highlight", HIGHLIGHT("outlet"), { "*": session }, 1);
      await turn(page, shot, sid, "panel", PANEL, { "*": session }, 1);
      await viewCall(page, shot, sid, "view-call", { "*": session }, 1);
      await turn(page, shot, sid, "connector-value", WHERE(NODE), { "*": session }, 1);
    }
  } finally {
    const all = await rows(sid);
    result.questions = asked(all).map((r) => `${r.tool_call?.tool_name} -> ${r.action || r.status}`);
    result.auto = all.filter((r) => r.status === "auto_approved").map((r) => `${r.tool_call?.tool_name}:${r.reason}`);
    result.unexpected = result.steps.filter((s) => s.unexpected).map((s) => `${s.name}: ${s.unexpected}`);
    console.log(`PERM_RESULT ${JSON.stringify(result)}`);
  }
}
