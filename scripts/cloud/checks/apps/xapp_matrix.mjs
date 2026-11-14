// The cross-app matrix (ledger "Cross-app matrix"): the generic MCP App features run on any
// app view, driven only by plain requests and the person's own controls.
//
//   XM_APP      a short name for the app (workspace folder, labels)
//   XM_OPEN     the plain request(s) that open the view; several turns are joined by " || ".
//               "{root}" is replaced by the conversation's workspace folder
//   XM_COPY     files copied into the workspace folder first (comma list, repository paths)
//   XM_WIDE=1   open the tab only once its frame is wider than 600 px (a view that picks a
//               narrow-screen layout by the frame's width when it loads)
//   XM_PHASES   comma list, in order (default all of card,tab,max,return,ctx,look,comment,theme,close):
//     card     the view in its card (feature 1)
//     tab      Open in tab; one frame for the view in the page (feature 1)
//     max      maximize and restore: a marker set in the view's window survives and no view
//              frame navigates (feature 2); the hero shot is taken maximized
//     return   Return to conversation puts the view back in its card, then Open in tab again
//     click    XM_CLICK, a selector inside the view, clicked as the person would
//     ask      XM_ASK, plain request(s) joined by " || " (e.g. one an app tool serves)
//     ctx      XM_CTX (default "What am I looking at?"): the context the view reported, and
//              whether the turn's answer used it (feature 3)
//     look     XM_LOOK (default "Look at the view and tell me what you see."): the tools the
//              agent called and its answer (feature 4)
//     comment  a region (XM_REGION "x,y,w,h", default 0.3,0.3,0.4,0.4) marked over the view
//              with XM_COMMENT, sent with no other text (feature 5)
//     theme    CLIO's dark and light theme: the view's body colours in each, hero shots
//              (feature 8)
//     close    the view closed through the close route (as the server does when it is
//              replaced), then Reopen from its card (feature 7)
//     restart  `stack.sh restart`, open the conversation again, Reopen or show the view
//              (feature 7); needs XM_SESSION
//     reopen   Reopen the conversation's latest view from its card (or its restored tab)
//   XM_SESSION  "<workspace id>/<session id>": continue that conversation
//   XM_PHONE=1  phone layout: no hero shots, the tab is the docked sheet
//   XM_OUT      a file that receives the JSON result (also printed as XM_RESULT)
import { copyFileSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import path from "node:path";
import { AGENT, ROOT, allowPending, api, openConversation, send } from "./_lib.mjs";

const env = process.env;
const phone = env.XM_PHONE === "1";
const out = { app: env.XM_APP, log: [], turns: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[xm] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};
const sleep = (page, ms) => page.waitForTimeout(ms);

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
    await sleep(page, 3000);
  }
  if (allowed.length) note("allowed", allowed);
}

async function messages(page) {
  const listed = await api("GET", `/v1/sessions/${sessionOf(page)}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  return [...rows].reverse(); // the API lists the newest first
}

async function lastTurn(page) {
  const rows = await messages(page);
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  const turn = rows.slice(lastUser + 1);
  const parts = turn.flatMap((m) => m.parts || []);
  return {
    calls: parts.filter((p) => p.type === "tool_call").map((p) => p.tool_name),
    views: parts.filter((p) => p.type === "mcp_app").map((p) => `${p.source_server}/${p.tool_name}`),
    answer: parts.filter((p) => p.type === "text").map((p) => p.text).join("\n").trim(),
  };
}

async function turn(page, shot, text, label) {
  await send(page, text);
  await sleep(page, 4000);
  await settle(page);
  const result = await lastTurn(page);
  out.turns.push({ label, text, ...result });
  note(`${label} calls`, result.calls);
  note(`${label} answer`, result.answer.slice(0, 2000));
  await sleep(page, 2000);
  await shot(label);
  return result;
}

/** The latest view part of the conversation: its instance id and data reference. */
async function latestView(page) {
  const parts = (await messages(page)).flatMap((m) => (m.parts || []).filter((p) => p.type === "mcp_app"));
  const part = parts.at(-1);
  if (!part) return null;
  return {
    id: part.app_instance_id || part.id,
    dataRef: part.data_ref,
    server: part.source_server,
    tool: part.tool_name,
  };
}

async function navCollapse(page) {
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (!navOpen) return;
  const toggle = page
    .locator('[data-slot="sidebar-trigger"]')
    .or(page.getByRole("button", { name: "Toggle Sidebar" }))
    .filter({ visible: true })
    .first();
  if (await toggle.count()) await toggle.click().catch(() => undefined);
  await sleep(page, 1200);
}

async function maximize(page) {
  const button = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await button.isVisible().catch(() => false)) await button.click();
  await sleep(page, 8000);
}

async function restore(page) {
  const button = page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).first();
  if (await button.isVisible().catch(() => false)) await button.click();
  await sleep(page, 5000);
}

async function hero(page, shot, name) {
  if (phone) return;
  await navCollapse(page);
  await maximize(page);
  await shot(name);
  await restore(page);
}

/** The frame of the view that is showing: the tab's when there is one, else the card's. */
async function showing(page) {
  for (const placement of ["tab", "surface", "inline"]) {
    const frame = page.locator(`[data-mcp-app-placement="${placement}"]`).filter({ visible: true });
    if (await frame.count()) return { placement, frame: frame.last() };
  }
  return null;
}

/** The view documents: each app frame's inner document (CLIO's sandbox frame holds it). */
function viewDocuments(page) {
  return page.frames().filter((f) => f.url() === "about:srcdoc" && /\/mcp-apps\//.test(f.parentFrame()?.url() || ""));
}

async function frameWidth(page, placement) {
  const box = await page.locator(`[data-mcp-app-placement="${placement}"] [data-mcp-app-iframe]`).first().boundingBox();
  return box ? Math.round(box.width) : 0;
}

async function openInTab(page) {
  await page.getByRole("button", { name: /^(Open in tab|Show tab)$/ }).filter({ visible: true }).last().click({ timeout: 30000 });
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 60000 });
}

async function widenCanvas(page, targetX) {
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" }).first();
  const box = await handle.boundingBox().catch(() => null);
  if (!box) return note("divider", "not found");
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move((x + targetX) / 2, y, { steps: 8 });
  await page.mouse.move(targetX, y, { steps: 8 });
  await page.mouse.up();
  await sleep(page, 1500);
}

async function returnToCard(page) {
  await page.getByRole("button", { name: "Return to conversation" }).filter({ visible: true }).first().click({ timeout: 20000 });
  await sleep(page, 4000);
}

async function tab(page, shot) {
  // A view that asked for fullscreen when it loaded is in its tab already.
  if (await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').count()) {
    note("tab", "the view opened in its tab by itself");
  } else await openInTab(page);
  if (env.XM_WIDE === "1" && !phone) {
    note("tab frame width at first load", await frameWidth(page, "tab"));
    await navCollapse(page);
    await widenCanvas(page, 560);
    await returnToCard(page);
    await openInTab(page);
  }
  await sleep(page, env.XM_WIDE === "1" ? 25000 : 8000);
  out.tabFrameWidth = await frameWidth(page, "tab");
  out.framesInPage = await page.locator("[data-mcp-app-iframe]").count();
  out.inTabRow = await page.locator("[data-mcp-app-in-tab]").count();
  note("tab", { width: out.tabFrameWidth, framesInPage: out.framesInPage, inTabRow: out.inTabRow });
  await shot("tab");
}

async function maxRestore(page, shot) {
  const docs = viewDocuments(page);
  for (const doc of docs) await doc.evaluate(() => { window.__xappMark = "kept"; }).catch(() => undefined);
  const navigations = [];
  let phase = "maximize";
  const listener = (f) => {
    if (f.url() === "about:srcdoc" || /\/mcp-apps\//.test(f.url())) navigations.push(`${phase}: ${f.url().slice(0, 60)}`);
  };
  page.on("framenavigated", listener);
  if (!phone) await navCollapse(page);
  await maximize(page);
  await sleep(page, 4000);
  await shot("hero");
  phase = "restore";
  await restore(page);
  page.off("framenavigated", listener);
  const kept = [];
  for (const doc of viewDocuments(page)) kept.push(await doc.evaluate(() => window.__xappMark || "lost").catch(() => "unreadable"));
  out.maximize = { marked: docs.length, kept, navigations };
  note("maximize and restore", out.maximize);
  await shot("restored");
}

async function returned(page, shot) {
  await returnToCard(page);
  out.afterReturn = {
    inline: await page.locator('[data-mcp-app-placement="inline"]').count(),
    tab: await page.locator('[data-mcp-app-placement="tab"]').count(),
    frames: await page.locator("[data-mcp-app-iframe]").count(),
  };
  note("returned to the card", out.afterReturn);
  await page.locator('[data-mcp-app-placement="inline"]').last().scrollIntoViewIfNeeded().catch(() => undefined);
  await sleep(page, 6000);
  await shot("returned");
  await openInTab(page);
  await sleep(page, env.XM_WIDE === "1" ? 25000 : 8000);
}

async function reported(page) {
  const meta = (await api("GET", `/v1/sessions/${sessionOf(page)}`)).metadata || {};
  return Object.values(meta.mcp_app_context || {}).map((row) => (row.context?.content || []).map((c) => c.text).join(" ").slice(0, 600));
}

async function comment(page, shot) {
  if (!phone) {
    await navCollapse(page);
    await maximize(page);
  }
  const view = await showing(page);
  if (!view) throw new Error("no app view is showing");
  await view.frame.getByRole("button", { name: "Comments" }).click();
  await sleep(page, 1500);
  const layer = view.frame.locator('[data-slot="comment-region-layer"]');
  const box = await layer.boundingBox();
  if (!box) throw new Error("the comment layer did not open over the view");
  const [x, y, w, h] = (env.XM_REGION || "0.3,0.3,0.4,0.4").split(",").map(Number);
  await page.mouse.move(box.x + x * box.width, box.y + y * box.height);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(box.x + (x + (w * i) / 12) * box.width, box.y + (y + (h * i) / 12) * box.height);
    await sleep(page, 25);
  }
  await page.mouse.up();
  const editor = page.getByPlaceholder("What should the agent notice here?");
  await editor.waitFor({ timeout: 20000 });
  await editor.fill(env.XM_COMMENT || "What is in this area?");
  await sleep(page, 5000); // the mark's snapshot
  await shot("comment-typed");
  await page.getByRole("button", { name: /^Done$/ }).click();
  await sleep(page, 1500);
  await page.getByRole("button", { name: /Add 1 mark to message/ }).click();
  await sleep(page, 1500);
  if (!phone) await restore(page);
  await shot("comment-attached");
  await turn(page, shot, "", "comment");
  await hero(page, shot, "comment-answer-hero");
}

async function bodyColours(page) {
  const rows = [];
  for (const doc of viewDocuments(page)) {
    rows.push(
      await doc
        .evaluate(() => {
          const body = getComputedStyle(document.body);
          return {
            background: body.backgroundColor,
            text: body.color,
            hostToken: getComputedStyle(document.documentElement).getPropertyValue("--color-background-primary").trim(),
            scheme: document.documentElement.style.colorScheme || "",
          };
        })
        .catch((e) => ({ error: String(e).slice(0, 120) })),
    );
  }
  return rows;
}

async function theme(page, shot) {
  const set = (id, dark) =>
    page.evaluate(([themeId, isDark]) => {
      document.documentElement.dataset.theme = themeId;
      document.documentElement.classList.toggle("dark", isDark);
    }, [id, dark]);
  out.theme = {};
  for (const [id, dark] of [["dark", true], ["light", false]]) {
    await set(id, dark);
    await sleep(page, 2500);
    out.theme[id] = await bodyColours(page);
    note(`theme ${id}`, out.theme[id]);
    if (phone) await shot(`theme-${id}`);
    else await hero(page, shot, `theme-${id}-hero`);
  }
  await set("dark", true);
}

async function closeAndReopen(page, shot) {
  const view = await latestView(page);
  note("closing", view);
  const sid = sessionOf(page);
  const query = new URLSearchParams({ data_ref: view.dataRef || "" });
  const response = await fetch(`${AGENT}/v1/sessions/${sid}/mcp-apps/${view.id}?${query}`, { method: "DELETE" });
  out.close = { status: response.status };
  await sleep(page, 5000);
  const line = page.locator("[data-mcp-app-reopen]").filter({ visible: true }).last();
  out.close.reopenOffered = await line.waitFor({ timeout: 30000 }).then(() => true, () => false);
  if (!out.close.reopenOffered) {
    note("reopen line", "not pushed; reloading the conversation");
    await page.reload();
    await sleep(page, 8000);
    out.close.reopenOfferedAfterReload = await line.waitFor({ timeout: 30000 }).then(() => true, () => false);
  }
  await line.scrollIntoViewIfNeeded().catch(() => undefined);
  await shot("closed-card");
  await reopen(page, shot, "reopened");
}

async function reopen(page, shot, label) {
  const line = page.locator("[data-mcp-app-reopen]").filter({ visible: true }).last();
  const tabRestored = await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').count();
  if (!tabRestored && (await line.count())) {
    await line.getByRole("button", { name: /Reopen/ }).click();
    const allow = page.getByRole("button", { name: /Allow once/ });
    if (await allow.first().waitFor({ timeout: 15000 }).then(() => true, () => false)) await allow.first().click();
  }
  const mounted = await page
    .locator("[data-mcp-app-iframe]")
    .first()
    .waitFor({ timeout: 120000 })
    .then(() => true, () => false);
  await sleep(page, env.XM_WIDE === "1" ? 25000 : 10000);
  const where = await showing(page);
  out[label] = { mounted, tabRestored: Boolean(tabRestored), placement: where?.placement };
  note(label, out[label]);
  await shot(label);
  await hero(page, shot, `${label}-hero`);
}

async function waitHttp(url, timeout = 600000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fetch(url).then((r) => r.ok).catch(() => false)) return;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

async function restart(page, shot) {
  const url = page.url();
  await sleep(page, 1500);
  await page.goto("about:blank");
  execSync("bash scripts/cloud/stack.sh restart", { stdio: "ignore", cwd: ROOT });
  await waitHttp(`${AGENT}/v1/health`);
  await waitHttp(`${AGENT}/v1/workspaces`);
  await sleep(page, 5000);
  // XM_RESTART_SESSIONS: other conversations ("<workspace id>/<session id>", comma list)
  // whose views are reopened after the same restart.
  const others = (env.XM_RESTART_SESSIONS || "").split(",").filter(Boolean);
  for (const [i, target] of [url, ...others].entries()) {
    const [workspace, session] = target.startsWith("http") ? [] : target.split("/");
    await page.goto(workspace ? new URL(`/workspaces/${workspace}/sessions/${session}`, url).toString() : target);
    await sleep(page, 10000);
    await reopen(page, shot, i ? `after-restart-${session}` : "after-restart");
  }
}

export default async function (page, shot) {
  const phases = (env.XM_PHASES || "card,tab,max,return,ctx,look,comment,theme,close").split(",");
  let root = "";
  if (env.XM_SESSION) {
    const [workspace, session] = env.XM_SESSION.split("/");
    await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
    await sleep(page, 8000);
  } else {
    ({ root } = await openConversation(page, `xapp-${env.XM_APP}${phone ? "-phone" : ""}`));
    for (const file of (env.XM_COPY || "").split(",").filter(Boolean)) {
      copyFileSync(path.join(ROOT, file), path.join(root, path.basename(file)));
    }
  }
  out.session = sessionOf(page);
  note("session", out.session);
  if (env.XM_OPEN && !env.XM_SESSION) {
    for (const [i, text] of env.XM_OPEN.split(" || ").entries()) {
      await turn(page, shot, text.replaceAll("{root}", root), `open${i + 1}`);
    }
    out.view = await latestView(page);
    await page.locator("[data-mcp-app-iframe]").first().waitFor({ timeout: 240000 });
    await sleep(page, 10000);
  }
  for (const phase of phases) {
    note("phase", phase);
    try {
      if (phase === "card") {
        await page.locator('[data-mcp-app-placement="inline"]').last().scrollIntoViewIfNeeded().catch(() => undefined);
        out.cardFrameWidth = await frameWidth(page, "inline");
        out.reported = await reported(page);
        note("card", { width: out.cardFrameWidth, reported: out.reported });
        await shot("card");
      } else if (phase === "tab") await tab(page, shot);
      else if (phase === "max") await maxRestore(page, shot);
      else if (phase === "return") await returned(page, shot);
      else if (phase === "click") {
        // The person works the view: XM_CLICK is a selector clicked inside the showing view.
        const where = await showing(page);
        await page
          .frameLocator(`[data-mcp-app-placement="${where.placement}"] [data-mcp-app-iframe]`)
          .last()
          .frameLocator("iframe")
          .locator(env.XM_CLICK)
          .first()
          .click({ timeout: 30000 });
        await sleep(page, 3000);
        out.reportedAfterClick = await reported(page);
        note("clicked", { selector: env.XM_CLICK, reported: out.reportedAfterClick });
        await shot("clicked");
      } else if (phase === "ask") {
        for (const [i, text] of (env.XM_ASK || "").split(" || ").filter(Boolean).entries()) {
          await turn(page, shot, text, `ask${i + 1}`);
        }
        await hero(page, shot, "ask-hero");
      } else if (phase === "ctx") {
        out.reported = await reported(page);
        await turn(page, shot, env.XM_CTX || "What am I looking at?", "ctx");
      } else if (phase === "look") {
        await turn(page, shot, env.XM_LOOK || "Look at the view and tell me what you see.", "look");
        await hero(page, shot, "look-hero");
      } else if (phase === "comment") await comment(page, shot);
      else if (phase === "theme") await theme(page, shot);
      else if (phase === "close") await closeAndReopen(page, shot);
      else if (phase === "restart") await restart(page, shot);
      else if (phase === "reopen") await reopen(page, shot, "reopened");
    } catch (error) {
      out[`${phase}Error`] = String(error).slice(0, 400);
      note(`${phase} failed`, out[`${phase}Error`]);
      await shot(`${phase}-failed`).catch(() => undefined);
    }
  }
  if (env.XM_OUT) writeFileSync(env.XM_OUT, JSON.stringify(out, null, 2));
  console.log(`XM_RESULT ${JSON.stringify(out)}`);
}
