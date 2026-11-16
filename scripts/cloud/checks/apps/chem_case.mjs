// Chemistry C-cases (docs/design/chemistry-in-clio-status.md): one conversation with an
// agent, driven turn by turn with plain requests and the person's own actions in the views.
//
// CASE_AGENT (default clio/alembic) and CASE_WORKSPACE (default chem) open a new
// conversation, or CASE_SESSION and CASE_WORKSPACE_ID resume one. CASE_STEPS is a JSON list:
//   {"send": "plain request"}          type it and wait for the turn (CASE_WAIT_MIN, default 15)
//   {"click": {"app": -1, "x": 120, "y": 80}}   click inside an app view (index into the app
//                                      frames on the page, -1 = last), at CSS pixels in it
//   {"drag": {"app": -1, "from": [x, y], "to": [x, y]}}
//   {"shot": "name"} / {"hero": "name"} a screenshot / a maximized-view screenshot
//   {"wait": seconds}
// Permission questions are answered with the broadest allow they offer and logged.
// Prints one JSON line: session, workspace, per turn the new tool calls and the answer.
import { api, appFrames, heroShot, openConversation, send, showLastApp, waitIdle } from "./_lib.mjs";

const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

async function messages(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/messages`);
  return Array.isArray(listed) ? listed : listed.messages || [];
}

/** What the agent did since the messages in `before`: tool calls, app views and its text. */
function turnOf(rows, before) {
  const fresh = rows.filter((m) => m.role === "assistant" && !before.has(m.id)).reverse();
  const parts = fresh.flatMap((m) => m.parts || []);
  return {
    tools: parts
      .filter((p) => p.type === "tool_call" || p.type === "mcp_app")
      .map((p) => (p.type === "mcp_app" ? `app:${p.tool_name}` : p.tool_name)),
    answer: words(parts.filter((p) => p.type === "text").map((p) => p.text).join(" ")),
  };
}

async function frameBox(page, index) {
  const frames = await appFrames(page);
  const row = frames.at(index);
  if (!row?.element) throw new Error(`no app frame ${index} (have ${frames.length})`);
  await row.element.scrollIntoViewIfNeeded().catch(() => {});
  return row.element.boundingBox();
}

export default async function (page, shot) {
  const report = { turns: [], log: [] };
  let sessionId = process.env.CASE_SESSION;
  if (sessionId) {
    const wsId = process.env.CASE_WORKSPACE_ID;
    await page.goto(new URL(`/workspaces/${wsId}/sessions/${sessionId}`, page.url()).toString(), {
      waitUntil: "domcontentloaded",
    });
    await page.waitForTimeout(6000);
    report.workspace = wsId;
  } else {
    const { session, workspace } = await openConversation(page, process.env.CASE_WORKSPACE || "chem");
    sessionId = session.id;
    report.workspace = workspace.id;
    await api("PATCH", `/v1/sessions/${sessionId}/agent`, { ref: process.env.CASE_AGENT || "clio/alembic" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForTimeout(5000);
  }
  report.session = sessionId;
  const steps = JSON.parse(process.env.CASE_STEPS || "[]");
  for (const step of steps) {
    if (step.send) {
      const before = new Set((await messages(sessionId)).map((m) => m.id));
      const started = Date.now();
      await send(page, step.send);
      await page.waitForTimeout(5000);
      const idle = await waitIdle(page, sessionId, report.log, Number(process.env.CASE_WAIT_MIN || 15));
      const turn = turnOf(await messages(sessionId), before);
      report.turns.push({
        sent: step.send,
        idle,
        seconds: Math.round((Date.now() - started) / 1000),
        tools: turn.tools,
        answer: turn.answer.slice(0, 2000),
      });
      report.apps = await showLastApp(page, await appFrames(page));
    } else if (step.click) {
      const box = await frameBox(page, step.click.app ?? -1);
      await page.mouse.click(box.x + step.click.x, box.y + step.click.y);
      await page.waitForTimeout(1500);
    } else if (step.drag) {
      const box = await frameBox(page, step.drag.app ?? -1);
      const [fx, fy] = step.drag.from;
      const [tx, ty] = step.drag.to;
      await page.mouse.move(box.x + fx, box.y + fy);
      await page.mouse.down();
      await page.mouse.move(box.x + tx, box.y + ty, { steps: 15 });
      await page.mouse.up();
      await page.waitForTimeout(1500);
    } else if (step.shot) {
      await shot(step.shot);
    } else if (step.hero) {
      await heroShot(page, shot, step.hero);
    } else if (step.wait) {
      await page.waitForTimeout(step.wait * 1000);
    }
  }
  report.frames = (await appFrames(page)).map(({ element, ...row }) => row);
  console.log(JSON.stringify(report));
}
