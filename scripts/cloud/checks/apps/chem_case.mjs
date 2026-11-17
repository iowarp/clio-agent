// Chemistry C-cases (docs/design/chemistry-in-clio-status.md): one conversation with an
// agent, driven turn by turn with plain requests and the person's own actions in the views.
//
// CASE_AGENT (default clio/alembic) and CASE_WORKSPACE (default chem) open a new
// conversation, or CASE_SESSION and CASE_WORKSPACE_ID resume one. CASE_STEPS is a JSON list:
//   {"send": "plain request"}          type it and wait for the turn (CASE_WAIT_MIN, default 15)
//   {"click": {"app": -1, "x": 120, "y": 80}}   click inside an app view (index into the app
//                                      frames on the page, -1 = last), at CSS pixels in it
//   {"drag": {"app": -1, "from": [x, y], "to": [x, y]}}
//   {"scroll": {"x": 1100, "y": 600, "dy": 500}}  wheel-scroll the page at a point
//   {"pageclick": {"x": 1100, "y": 600}}  click the page itself (a chart, a button)
//   {"press": "#region"}               click an element inside a view, by selector
//   {"viewdrag": {"in": "#overlay", "from": [fx, fy], "to": [fx, fy]}}  drag inside a view's
//                                      element, at fractions 0..1 of its box
//   {"pickatoms": [0, 1, 2, 3]}        click those atoms (file order) in the Burette view, as
//                                      a person clicks them, where the viewer draws them
//   {"fill": {"in": "#edit-value", "text": "60"}}  type into a view's input
//   {"attach": "/path/figure.png"}     add a file to the composer (sent with the next send)
//   {"pressnear": {"sel": "[data-testid=bond]", "x": 498, "y": 219}}  click, among a
//                                      view's elements matching sel, the one nearest that point
//                                      in the view's own CSS pixels (a bond in the sketcher);
//                                      "dry": true only logs the elements' view positions
//   {"secret": "CASE_SECRET"}          type the value of that environment variable into the
//                                      setup card's masked key field (refused unless the field
//                                      is a password input; the value is never logged)
//   {"button": "Save"}                 click the page's button with that name
//   {"settle": true}                   wait for the running turn to finish and record it
//   {"chartdrag": {"ticks": [0, 50], "from": -5, "to": 5}}  drag across the last chart on the
//                                      page between two x values, placed from two x-axis tick
//                                      labels it draws
//   {"elshot": {"app": -1, "path": "site/src/assets/tutorials/chemistry/x.png"}}  screenshot
//                                      one app view's frame as an element (at --scale)
//   {"allow": true}                    answer pending permission prompts now (a view's own
//                                      call, such as an edit's Apply, asks before it runs)
//   {"pagedrag": {"from": [x, y], "to": [x, y]}}  drag on the page at page coordinates
//   {"shot": "name"} / {"hero": "name"} a screenshot / a maximized-view screenshot
//   {"wait": seconds}
// Permission questions are answered with the broadest allow they offer and logged. The
// agent's own questions are answered from CASE_ANSWERS, a JSON map from a word in the
// question to the option the person picks; unmatched questions stay pending.
// Prints one JSON line: session, workspace, per turn the new tool calls and the answer.
import { allowPending, api, appFrames, heroShot, openConversation, send, showLastApp, waitIdle } from "./_lib.mjs";

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

/** The view frame (inside an app's sandbox) that holds `selector`. */
async function viewFrame(page, selector) {
  for (const frame of page.frames()) {
    if ((await frame.locator(selector).count().catch(() => 0)) > 0) return frame;
  }
  throw new Error(`no view holds ${selector}`);
}

async function frameBox(page, index) {
  const frames = await appFrames(page);
  const row = frames.at(index);
  if (!row?.element) throw new Error(`no app frame ${index} (have ${frames.length})`);
  await row.element.scrollIntoViewIfNeeded().catch(() => {});
  return row.element.boundingBox();
}

const log = (report, line) => report.log.push(line);

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
      const idle = await waitIdle(
        page,
        sessionId,
        report.log, Number(process.env.CASE_WAIT_MIN || 15),
        JSON.parse(process.env.CASE_ANSWERS || "null"),
      );
      let turn = turnOf(await messages(sessionId), before);
      // The session can read idle before the reply starts (a view's call settling first):
      // keep waiting until the reply is there.
      for (let k = 0; k < 3 && !turn.answer && !turn.tools.length; k += 1) {
        await page.waitForTimeout(10000);
        await waitIdle(page, sessionId, report.log, Number(process.env.CASE_WAIT_MIN || 15),
          JSON.parse(process.env.CASE_ANSWERS || "null"));
        turn = turnOf(await messages(sessionId), before);
      }
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
    } else if (step.scroll) {
      await page.mouse.move(step.scroll.x, step.scroll.y);
      await page.mouse.wheel(0, step.scroll.dy);
      await page.waitForTimeout(1500);
    } else if (step.press) {
      const frame = await viewFrame(page, step.press);
      await frame.locator(step.press).first().click();
      await page.waitForTimeout(1500);
    } else if (step.viewdrag) {
      const frame = await viewFrame(page, step.viewdrag.in);
      const outer = await frame.parentFrame()?.frameElement();
      await outer?.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await page.waitForTimeout(800);
      const box = await frame.locator(step.viewdrag.in).first().boundingBox();
      const at = ([fx, fy]) => [box.x + fx * box.width, box.y + fy * box.height];
      await page.mouse.move(...at(step.viewdrag.from));
      await page.mouse.down();
      await page.mouse.move(...at(step.viewdrag.to), { steps: 15 });
      await page.mouse.up();
      await page.waitForTimeout(1500);
    } else if (step.pickatoms) {
      const frame = await viewFrame(page, "#stage");
      const outer = await frame.parentFrame()?.frameElement();
      await outer?.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await page.waitForTimeout(800);
      const origin = await (await frame.frameElement()).boundingBox();
      for (const index of step.pickatoms) {
        const at = await frame.evaluate((i) => window.__buretteViewer.screenOf(i), index);
        await page.mouse.click(origin.x + at.x, origin.y + at.y);
        await page.waitForTimeout(700);
      }
      await page.waitForTimeout(1000);
    } else if (step.fill) {
      const frame = await viewFrame(page, step.fill.in);
      await frame.locator(step.fill.in).first().fill(String(step.fill.text));
      await frame.locator(step.fill.in).first().press("Enter");
      await page.waitForTimeout(1000);
    } else if (step.attach) {
      await page.locator('input[type="file"][title="Upload files"]').first().setInputFiles(step.attach);
      await page.waitForTimeout(2500);
    } else if (step.pressnear) {
      const { sel, x, y, dry } = step.pressnear;
      const frame = await viewFrame(page, sel);
      const outer = await frame.parentFrame()?.frameElement();
      await outer?.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await page.waitForTimeout(800);
      const centers = await frame.locator(sel).evaluateAll((els) =>
        els.map((e) => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }));
      const origin = await (await frame.frameElement()).boundingBox();
      const dist = ([cx, cy]) => Math.hypot(cx - x, cy - y);
      const best = centers.reduce((b, c, i) => (dist(c) < dist(centers[b]) ? i : b), 0);
      log(report, `pressnear ${sel}: ${JSON.stringify(centers.map((c) => c.map(Math.round)))} -> ${best}`);
      if (!dry) await page.mouse.click(origin.x + centers[best][0], origin.y + centers[best][1]);
      await page.waitForTimeout(1500);
    } else if (step.secret) {
      const field = page.locator('input[type="password"]').last();
      if ((await field.count()) === 0) throw new Error("no masked key field on the page");
      const value = process.env[step.secret];
      if (!value) throw new Error(`${step.secret} is not set`);
      await field.fill(value);
      log(report, `typed ${step.secret} into the masked key field`);
    } else if (step.button) {
      await page.getByRole("button", { name: step.button, exact: false }).last().click();
      await page.waitForTimeout(2000);
    } else if (step.settle) {
      const idle = await waitIdle(page, sessionId, report.log, Number(process.env.CASE_WAIT_MIN || 15),
        JSON.parse(process.env.CASE_ANSWERS || "null"));
      // The running turn's reply is the assistant messages after the last user message.
      const rows = await messages(sessionId);
      const lastUser = rows.findIndex((m) => m.role === "user");
      const turn = turnOf(rows, new Set(rows.slice(lastUser).map((m) => m.id)));
      report.turns.push({ sent: "(settled after the card)", idle, tools: turn.tools, answer: turn.answer.slice(0, 2000) });
    } else if (step.chartdrag) {
      const { ticks, from, to } = step.chartdrag;
      // The transcript mounts only what is near the screen: scroll up from the bottom
      // until a chart with these ticks is drawn (the newest one is met first).
      const locate = (labels) => {
        const charts = [...document.querySelectorAll("svg")].filter((svg) => {
          const texts = [...svg.querySelectorAll("text")].map((t) => t.textContent.trim());
          return labels.every((l) => texts.includes(String(l)));
        });
        const svg = charts.at(-1);
        if (!svg) return null;
        svg.scrollIntoView({ block: "center" });
        const box = svg.getBoundingClientRect();
        const at = labels.map((l) => {
          const nodes = [...svg.querySelectorAll("text")].filter((t) => t.textContent.trim() === String(l));
          const r = nodes.map((t) => t.getBoundingClientRect()).sort((a, b) => b.y - a.y)[0];
          return { x: r.x + r.width / 2, y: r.y };
        });
        return { at, top: box.top };
      };
      let found = null;
      for (let k = 0; k < 25 && !found; k += 1) {
        found = await page.evaluate(locate, ticks);
        if (!found) {
          // Scroll over the transcript's margin: a view under the pointer takes the wheel.
          await page.mouse.move(370, 500);
          await page.mouse.wheel(0, -500);
          await page.waitForTimeout(700);
        }
      }
      if (!found) throw new Error(`no chart with x ticks ${ticks}`);
      await page.waitForTimeout(800);
      found = await page.evaluate(locate, ticks);
      const [a, b] = found.at;
      const px = (v) => a.x + ((v - ticks[0]) * (b.x - a.x)) / (ticks[1] - ticks[0]);
      const y = (found.top + a.y) / 2;
      log(report, `chartdrag ${from}..${to} at x ${Math.round(px(from))}..${Math.round(px(to))}, y ${Math.round(y)}`);
      await page.mouse.move(px(from), y);
      await page.mouse.down();
      await page.mouse.move(px(to), y, { steps: 12 });
      await page.mouse.up();
      await page.waitForTimeout(2000);
    } else if (step.elshot) {
      let frames = await appFrames(page);
      for (let k = 0; k < 90 && !frames.length; k += 1) {
        await page.mouse.move(370, 500);  // over the margin: a view takes the wheel
        await page.mouse.wheel(0, -600);
        await page.waitForTimeout(700);
        frames = await appFrames(page);
      }
      await page.waitForTimeout(2500);
      frames = await appFrames(page);
      const row = frames.at(step.elshot.app ?? -1);
      if (!row?.element) throw new Error("no app frame to capture");
      await row.element.evaluate((el) => el.scrollIntoView({ block: "center" }));
      await page.waitForTimeout(1500);
      await row.element.screenshot({ path: step.elshot.path });
      log(report, `captured ${step.elshot.path}`);
    } else if (step.allow) {
      for (let k = 0; k < 10; k += 1) {
        await allowPending(sessionId, report.log);
        await page.waitForTimeout(1000);
      }
    } else if (step.pagedrag) {
      await page.mouse.move(...step.pagedrag.from);
      await page.mouse.down();
      await page.mouse.move(...step.pagedrag.to, { steps: 15 });
      await page.mouse.up();
      await page.waitForTimeout(1500);
    } else if (step.pageclick) {
      await page.mouse.click(step.pageclick.x, step.pageclick.y);
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
