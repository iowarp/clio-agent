// Screenshots for the CLIO website: each scene shot in the light and the dark theme.
// Run with --scale 2 at 1440x900 (or 390x844 --touch for the phone shots). The left rail
// stays open on desktop so the CLIO logo shows. Requests are plain and name no tool.
//
//   WS_MODE       landing | offer | shoot | comment | dashboard
//   WS_WORKSPACE  workspace name for a new conversation (offer), e.g. wave-study
//   WS_SESSION    "<workspace id>/<session id>" to continue (shoot, comment, dashboard)
//   WS_REQUEST    the plain request (offer, dashboard)
//   WS_ADD        "0": shoot the offer card and leave it waiting (default: press Add)
//   WS_TAB        "1": put the newest view in a tab first (shoot, comment)
//   WS_WIDTH      the tab's share of the window when widened (e.g. 0.58)
//   WS_MAX        "1": maximize the canvas (shoot); WS_SCROLL pixels to scroll it
//   WS_REMOTE     "1": the view's own "Render on ParaView machine" before the shot (shoot)
//   WS_OPEN       text of an output card to open first (shoot), e.g. a dashboard's title
//   WS_COMMENT    the comment typed on the region (comment); WS_REGION "x,y,w,h" fractions
//   WS_SEND       "1": send the marked comment and shoot the answer too (comment)
//   WS_NAME       the scene name used in the shot names
// Every scene is shot as <name>-dark and <name>-light. One JSON line reports the session.
import { allowPending, api } from "./_lib.mjs";

const env = process.env;
const name = env.WS_NAME || env.WS_MODE || "scene";
const report = { mode: env.WS_MODE, shots: [] };

/** What the UI's own theme switch does to <html> (theme/preference.ts switchTheme). */
async function setTheme(page, dark) {
  await page.evaluate((isDark) => {
    const root = document.documentElement;
    root.dataset.themeSwitching = "";
    root.dataset.theme = isDark ? "dark" : "light";
    root.classList.toggle("dark", isDark);
    requestAnimationFrame(() => requestAnimationFrame(() => delete root.dataset.themeSwitching));
  }, dark);
  await page.waitForTimeout(2500);
}

async function both(page, shot, scene, before) {
  for (const theme of ["dark", "light"]) {
    await setTheme(page, theme === "dark");
    if (before) await before(theme);
    report.shots.push(await shot(`${scene}-${theme}`));
  }
  await setTheme(page, true);
}

async function settle(page, sid, minutes = 12) {
  const until = Date.now() + minutes * 60_000;
  const log = [];
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sid, log);
    const session = await api("GET", `/v1/sessions/${sid}`);
    quiet = ["idle", "completed", "error"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(3000);
  }
  return false;
}

async function composerSend(page, text) {
  const composer = page.locator('[data-slot="clio-composer"] [contenteditable="true"], textarea').first();
  await composer.waitFor({ state: "visible", timeout: 120000 });
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

async function goSession(page, path) {
  const [workspace, session] = path.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(8000);
  return session;
}

async function toTab(page) {
  if (await page.locator('[data-mcp-app-placement="tab"]').filter({ visible: true }).count()) return;
  const open = page.getByRole("button", { name: /Open in tab|Show tab|Reopen/ }).filter({ visible: true });
  if (await open.count()) await open.last().click({ timeout: 10000 }).catch(() => undefined);
  for (let i = 0; i < 30; i += 1) {
    if (await page.locator('[data-mcp-app-placement="tab"]').filter({ visible: true }).count()) break;
    await page.waitForTimeout(2000);
  }
  await page.waitForTimeout(12000);
}

/** Drag the divider between the conversation and the workbench to a share of the window. */
async function widen(page, share) {
  if (!share) return;
  const handle = page.locator('[data-slot="resizable-handle"], [role="separator"][aria-orientation="vertical"]').filter({ visible: true }).last();
  const box = await handle.boundingBox().catch(() => null);
  if (!box) return;
  const width = page.viewportSize().width;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  const target = width * (1 - share);
  for (let i = 1; i <= 15; i += 1) await page.mouse.move(x + ((target - x) * i) / 15, y);
  await page.mouse.up();
  await page.waitForTimeout(8000);
}

export default async function (page, shot) {
  const mode = env.WS_MODE;
  if (mode === "landing") {
    await page.waitForTimeout(4000);
    await both(page, shot, name);
  } else if (mode === "offer") {
    const workspaces = await api("GET", "/v1/workspaces");
    const rows = Array.isArray(workspaces) ? workspaces : workspaces.workspaces || workspaces.items || [];
    const workspace = rows.find((w) => w.name === env.WS_WORKSPACE);
    if (!workspace) throw new Error(`no workspace ${env.WS_WORKSPACE}`);
    const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id, approval_mode: "bypass" });
    report.session = `${workspace.id}/${session.id}`;
    await goSession(page, report.session);
    await composerSend(page, env.WS_REQUEST);
    const card = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"]:visible').last();
    await card.waitFor({ state: "visible", timeout: 600000 });
    await page.waitForTimeout(3000);
    await both(page, shot, `${name}-offer-card`);
    if (env.WS_ADD === "0") return console.log(JSON.stringify(report));
    await card.getByRole("button", { name: /^1?\s*Add$/ }).click();
    report.settled = await settle(page, session.id);
    await page.waitForTimeout(4000);
    await toTab(page);
    await widen(page, Number(env.WS_WIDTH || 0));
    await both(page, shot, `${name}-view`);
  } else if (mode === "shoot") {
    report.session = env.WS_SESSION;
    await goSession(page, env.WS_SESSION);
    if (env.WS_TAB === "1") await toTab(page);
    if (env.WS_OPEN) {
      // Open an output (a dashboard) from its card in the conversation.
      await page.getByText(env.WS_OPEN, { exact: false }).last().click();
      await page.waitForTimeout(20000);
    }
    await widen(page, Number(env.WS_WIDTH || 0));
    if (env.WS_MAX === "1") {
      // The canvas over the conversation; the left rail (and the CLIO logo) stays.
      const maximize = page.getByRole("button", { name: "Maximize canvas" }).filter({ visible: true }).first();
      if (await maximize.count()) await maximize.click({ timeout: 10000 }).catch(() => undefined);
      await page.waitForTimeout(15000);
    }
    if (env.WS_REMOTE === "1") {
      // The view draws on the ParaView machine (its own control), not in the browser.
      for (const frame of page.frames()) {
        if (!/\/mcp-apps\//.test(frame.url())) continue;
        const remote = frame.getByRole("button", { name: "Render on ParaView machine" });
        if (await remote.count()) await remote.first().click().catch(() => undefined);
      }
      // Leave the control by moving across the view, then out to an empty part of the
      // rail, so the view's own tooltip is not in the shot.
      const { width: vw, height: vh } = page.viewportSize();
      for (const [x, y] of [[0.5, 0.75], [0.45, 0.8], [0.1, 0.6], [0.08, 0.62]])
        await page.mouse.move(vw * x, vh * y, { steps: 8 });
      await page.waitForTimeout(2500);
      await page.waitForTimeout(12000);
    }
    if (env.WS_SCROLL) {
      // Scroll the canvas's scrolling panel so the wanted part of a surface shows.
      await page.mouse.move(page.viewportSize().width * 0.7, page.viewportSize().height * 0.6);
      await page.mouse.wheel(0, Number(env.WS_SCROLL));
      await page.waitForTimeout(4000);
    }
    await both(page, shot, name);
  } else if (mode === "dashboard") {
    report.session = env.WS_SESSION;
    const sid = await goSession(page, env.WS_SESSION);
    await composerSend(page, env.WS_REQUEST);
    await page.waitForTimeout(5000);
    report.settled = await settle(page, sid, 20);
    await page.waitForTimeout(15000);
    await widen(page, Number(env.WS_WIDTH || 0));
    await both(page, shot, name);
  } else if (mode === "comment") {
    report.session = env.WS_SESSION;
    const sid = await goSession(page, env.WS_SESSION);
    if (env.WS_TAB === "1") await toTab(page);
    await widen(page, Number(env.WS_WIDTH || 0));
    const frame = page.locator('[data-mcp-app-placement="tab"]').filter({ visible: true }).last();
    await frame.getByRole("button", { name: "Comments" }).click();
    await page.waitForTimeout(1500);
    const layer = frame.locator('[data-slot="comment-region-layer"]');
    const box = await layer.boundingBox();
    if (!box) throw new Error("the comment layer did not open over the view");
    const [x, y, w, h] = (env.WS_REGION || "0.35,0.3,0.3,0.35").split(",").map(Number);
    await page.mouse.move(box.x + x * box.width, box.y + y * box.height);
    await page.mouse.down();
    for (let i = 1; i <= 12; i += 1) {
      await page.mouse.move(box.x + (x + (w * i) / 12) * box.width, box.y + (y + (h * i) / 12) * box.height);
      await page.waitForTimeout(25);
    }
    await page.mouse.up();
    const editor = page.getByPlaceholder("What should the agent notice here?");
    await editor.waitFor({ timeout: 20000 });
    await editor.fill(env.WS_COMMENT);
    await page.waitForTimeout(5000);
    await both(page, shot, `${name}-typed`);
    if (env.WS_SEND === "1") {
      await page.getByRole("button", { name: /^Done$/ }).click();
      await page.waitForTimeout(1500);
      await page.getByRole("button", { name: /Add 1 mark to message/ }).click();
      await page.waitForTimeout(1500);
      // The marks wait in the composer; send them from there.
      await page.locator('[data-slot="clio-composer"] [contenteditable="true"]').first().click();
      await page.keyboard.press("Enter");
      await page.waitForTimeout(5000);
      report.settled = await settle(page, sid);
      await page.waitForTimeout(5000);
      await both(page, shot, `${name}-answer`);
    }
  } else {
    throw new Error(`unknown WS_MODE ${mode}`);
  }
  console.log(JSON.stringify(report));
}
