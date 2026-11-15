// The agent looks at an app view, and the person comments on it (CM1 to CM3).
//
// Plain requests only. Environment:
//   CM_NAME     workspace folder name under .cloud-run/apps-work/ (default comments-look)
//   CM_SESSION  "<workspace id>/<session id>": continue that conversation instead of a new one
//   CM_SETTLE   "1": first wait for a turn already running in the conversation to end
//   CM_OPEN     the request that opens the view (skipped when empty)
//   CM_TAB      "1": move the card's view into a tab when it is not in one
//   CM_LOOK     the request that should make the agent look (skipped when empty)
//   CM_COMMENT  the comment typed on the marked region (skipped when empty)
//   CM_REGION   "x,y,w,h" fractions of the view for the region (default 0.35,0.3,0.3,0.35)
//   CM_POINT    "x,y": click a point instead of dragging a region
//   CM_PHONE    "1": a phone-sized run (no hero layout)
//   CM_SEND     the text sent with the marks (default: none, the marks alone)
//   CM_OUT      a file that receives the session id and the turns' tool calls and answers
// It waits for each turn to end, allowing the session's permission asks as a person
// would, takes a normal shot and a hero shot (navigation collapsed, workbench maximized)
// after the look and after the comment's answer, and records what the agent called.
import { writeFileSync } from "node:fs";
import { allowPending, api, openConversation, send } from "./_lib.mjs";

const env = process.env;
const out = { log: [], turns: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[cm] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

function sessionOf(page) {
  return decodeURIComponent(new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/)[1]);
}

async function settle(page, minutes = 15) {
  const sid = sessionOf(page);
  const until = Date.now() + minutes * 60_000;
  const allowed = [];
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sid, allowed);
    const session = await api("GET", `/v1/sessions/${sid}`);
    quiet = ["idle", "completed", "error"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) break;
    await page.waitForTimeout(3000);
  }
  if (allowed.length) note("allowed", allowed);
}

async function lastTurn(page) {
  const listed = await api("GET", `/v1/sessions/${sessionOf(page)}/messages`);
  const listedRows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  // The API lists the newest message first.
  const rows = [...listedRows].reverse();
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  const turn = rows.slice(lastUser + 1);
  const calls = turn.flatMap((m) =>
    (m.parts || []).filter((p) => p.type === "tool_call").map((p) => p.tool_name),
  );
  const answer = turn
    .flatMap((m) => (m.parts || []).filter((p) => p.type === "text").map((p) => p.text))
    .join("\n")
    .trim();
  return { calls, answer };
}

/** The hero layout: the navigation collapsed and the workbench maximized. */
async function heroOn(page) {
  const navOpen = await page
    .getByRole("button", { name: "New conversation" })
    .first()
    .isVisible()
    .catch(() => false);
  if (navOpen) {
    const toggle = page
      .locator('[data-slot="sidebar-trigger"]')
      .or(page.getByRole("button", { name: "Toggle Sidebar" }))
      .filter({ visible: true })
      .first();
    if (await toggle.count()) await toggle.click().catch(() => undefined);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.isVisible().catch(() => false)) await maximize.click();
  await page.waitForTimeout(8000);
}

async function heroOff(page) {
  const restore = page
    .getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ })
    .first();
  if (await restore.isVisible().catch(() => false)) await restore.click();
  await page.waitForTimeout(6000);
}

async function hero(page, shot, name) {
  await heroOn(page);
  await shot(name);
  await heroOff(page);
}

/** The frame of the app view that is showing: the tab's when there is one, else the card's. */
async function appFrame(page) {
  for (const placement of ["tab", "surface", "inline"]) {
    const frame = page.locator(`[data-mcp-app-placement="${placement}"]`).filter({ visible: true });
    if (await frame.count()) return frame.last();
  }
  return null;
}

async function turn(page, shot, text, label) {
  await send(page, text);
  await page.waitForTimeout(4000);
  await settle(page);
  const result = await lastTurn(page);
  out.turns.push({ label, text, ...result });
  note(`${label} calls`, result.calls);
  note(`${label} answer`, result.answer.slice(0, 1500));
  await page.waitForTimeout(2000);
  await shot(label);
}

export default async function (page, shot) {
  // The page's own warnings about views and snapshots go to the log.
  page.on("console", (message) => {
    const text = message.text();
    if (/view|snapshot|look/i.test(text)) note(`console ${message.type()}`, text.slice(0, 400));
  });
  if (env.CM_SESSION) {
    const [workspace, session] = env.CM_SESSION.split("/");
    await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
    await page.waitForTimeout(6000);
  } else {
    await openConversation(page, env.CM_NAME || "comments-look");
  }
  out.session = sessionOf(page);
  note("session", out.session);

  if (env.CM_SETTLE === "1") {
    // A turn still running in this conversation (a look waiting for a surface): let it end.
    await page.waitForTimeout(8000);
    await settle(page);
    const result = await lastTurn(page);
    out.turns.push({ label: "settled", ...result });
    note("settled calls", result.calls);
    note("settled answer", result.answer.slice(0, 1500));
    await shot("settled");
  }
  if (env.CM_OPEN) {
    await turn(page, shot, env.CM_OPEN, "open");
    await page.locator("[data-mcp-app-iframe]").first().waitFor({ timeout: 240000 });
    await page.waitForTimeout(8000);
  }
  if (env.CM_TAB === "1" && !(await page.locator('[data-mcp-app-placement="tab"]').count())) {
    const open = page.getByRole("button", { name: /Open in tab|Show tab|Reopen/ }).filter({ visible: true });
    if (await open.count()) await open.last().click({ timeout: 10000 }).catch(() => undefined);
    await page.waitForTimeout(10000);
  }
  // A view being reopened (its saved call replays) takes a while to mount.
  for (let i = 0; i < 36 && !(await appFrame(page)); i += 1) await page.waitForTimeout(5000);
  await page.waitForTimeout(8000);
  if (env.CM_LOOK) {
    await turn(page, shot, env.CM_LOOK, "look");
    if (!env.CM_PHONE) await hero(page, shot, "look-hero");
  }
  if (!env.CM_COMMENT) {
    if (env.CM_OUT) writeFileSync(env.CM_OUT, JSON.stringify(out, null, 2));
    return;
  }

  // The comment is made in the hero layout (maximizing remounts the view, so it comes first).
  if (!env.CM_PHONE) await heroOn(page);
  const frame = await appFrame(page);
  if (!frame) throw new Error("no app view is showing");
  await frame.getByRole("button", { name: "Comments" }).click();
  await page.waitForTimeout(1500);
  const layer = frame.locator('[data-slot="comment-region-layer"]');
  const box = await layer.boundingBox();
  if (!box) throw new Error("the comment layer did not open over the view");
  if (env.CM_POINT) {
    const [px, py] = env.CM_POINT.split(",").map(Number);
    await page.mouse.click(box.x + px * box.width, box.y + py * box.height);
  } else {
    const [x, y, w, h] = (env.CM_REGION || "0.35,0.3,0.3,0.35").split(",").map(Number);
    await page.mouse.move(box.x + x * box.width, box.y + y * box.height);
    await page.mouse.down();
    for (let i = 1; i <= 12; i += 1) {
      await page.mouse.move(
        box.x + (x + (w * i) / 12) * box.width,
        box.y + (y + (h * i) / 12) * box.height,
      );
      await page.waitForTimeout(25);
    }
    await page.mouse.up();
  }
  const editor = page.getByPlaceholder("What should the agent notice here?");
  await editor.waitFor({ timeout: 20000 });
  await editor.fill(env.CM_COMMENT);
  await page.waitForTimeout(5000); // the mark's snapshot
  await shot("comment-typed");
  await page.getByRole("button", { name: /^Done$/ }).click();
  await page.waitForTimeout(1500);
  await shot("comment-marked");
  await page.getByRole("button", { name: /Add 1 mark to message/ }).click();
  await page.waitForTimeout(1500);
  if (!env.CM_PHONE) await heroOff(page);
  await shot("comment-attached");
  if (env.CM_PHONE) {
    // On a phone the canvas covers the conversation: close it to reach the composer.
    const close = page.getByRole("button", { name: "Close workspace canvas" }).filter({ visible: true });
    if (await close.count()) await close.first().click().catch(() => undefined);
    else await page.keyboard.press("Control+Shift+B"); // the canvas toggle's shortcut
    await page.waitForTimeout(2000);
    await shot("comment-in-composer");
  }
  await turn(page, shot, env.CM_SEND || "", "comment");
  if (!env.CM_PHONE) await hero(page, shot, "comment-answer-hero");
  if (env.CM_OUT) writeFileSync(env.CM_OUT, JSON.stringify(out, null, 2));
}
