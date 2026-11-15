// Slicer plugin (SL): a person uses the layer view of a slice in a conversation, and the
// view is shot as a hero (navigation collapsed, canvas maximized). The view is clio-kit's
// slicer server's MCP App (ui://slicer/layers); everything here is what a person does.
//
//   SL_SESSION  "<workspace id>/<session id>" holding the view
//   SL_STEPS    comma list of: slider (drag the layer slider with the keyboard), toggle
//               (hide sparse infill, show travel, then restore), pick (click a toolpath),
//               hero (the hero shot) — default "slider,toggle,pick,hero"
//   SL_SEND     a plain follow-up typed once the view is open (optional); the turn settles
// Prints the view's state after each step (layer, z_mm, visible_features, selection) and
// the text the view shows, so the shots are checked against what the view reports.
import { allowPending, api, send } from "./_lib.mjs";

const env = process.env;
const out = { steps: [] };
const note = (label, value) => {
  out.steps.push({ label, value });
  console.error(`[sl] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

async function onScreen(page, frame) {
  const box = await (await frame.frameElement()).boundingBox().catch(() => null);
  const view = page.viewportSize();
  return !!box && box.width > 40 && box.height > 40 && box.y < view.height && box.y + box.height > 0 && box.x < view.width;
}

async function findView(page, minutes = 3) {
  for (let i = 0; i < minutes * 60; i++) {
    const frames = [];
    for (const frame of page.frames()) {
      if (frame === page.mainFrame()) continue;
      const ready = await frame
        .evaluate(() => !!(window.slicerView && window.slicerView.state) && document.getElementById("status").hidden)
        .catch(() => false);
      if (ready) frames.push(frame);
    }
    for (const frame of frames.reverse()) if (await onScreen(page, frame)) return frame;
    if (i % 5 === 4 && frames.length) {
      const element = await frames[0].frameElement();
      await element.scrollIntoViewIfNeeded().catch(() => undefined);
    }
    await page.waitForTimeout(1000);
  }
  throw new Error("no slice view on screen");
}

const viewState = (frame) =>
  frame.evaluate(() => ({
    state: window.slicerView.state,
    layerText: document.getElementById("layer-text").textContent,
    stats: document.getElementById("stats").innerText,
    warnings: document.getElementById("warnings").hidden ? null : document.getElementById("warning-list").innerText,
  }));

async function heroOn(page) {
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (navOpen) {
    const toggle = page.locator('[data-slot="sidebar-trigger"]').or(page.getByRole("button", { name: "Toggle Sidebar" }))
      .filter({ visible: true }).first();
    if (await toggle.count()) await toggle.click().catch(() => undefined);
  }
  // The newest view in a card moves to a workbench tab first ("Open in tab" on its card),
  // so the hero shows the view the conversation last opened, not an older tab.
  const open = page.getByRole("button", { name: /^Open in tab/ }).filter({ visible: true }).last();
  if (await open.count()) {
    await open.click().catch(() => undefined);
    await page.waitForTimeout(4000);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if ((await page.locator("[data-mcp-app-tab]:visible").count()) && (await maximize.count())) {
    await maximize.click().catch(() => undefined);
  }
  await page.waitForTimeout(5000);
}

async function settleTurn(page, session) {
  const log = [];
  const until = Date.now() + 10 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    await allowPending(session, log);
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    quiet = ["idle", "completed"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  if (log.length) note("permissions", log);
}

export default async function (page, shot) {
  const [workspace, session] = env.SL_SESSION.split("/");
  const steps = (env.SL_STEPS || "slider,toggle,pick,hero").split(",");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  // A view whose window closed shows "Reopen" on its card: a person presses it.
  let view = await findView(page, 0.25).catch(() => null);
  if (!view) {
    const reopen = page.getByRole("button", { name: /^(Reopen|Show tab)/ }).filter({ visible: true }).last();
    await reopen.scrollIntoViewIfNeeded().catch(() => undefined);
    await reopen.click();
    note("reopen", "pressed Reopen or Show tab on the view's card");
    await page.waitForTimeout(4000);
    view = await findView(page);
  }
  note("open", await viewState(view));
  await shot("view");
  // The follow-up goes in with the view open, so the agent can move the live view.
  if (env.SL_SEND) {
    await send(page, env.SL_SEND);
    await page.waitForTimeout(5000);
    await settleTurn(page, session);
    await page.waitForTimeout(4000);
    await shot("answered");
    view = await findView(page);
    note("after-send", await viewState(view));
  }
  if (steps.includes("slider")) {
    // The person drags the "Show layer" slider down with the keyboard (Home, then up).
    const slider = view.locator("#to");
    await slider.focus();
    await page.keyboard.press("Home");
    for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(1500);
    note("slider", await viewState(view));
    await shot("slider");
  }
  if (steps.includes("toggle")) {
    // On a phone the panel starts folded: the person opens it first.
    if ((await view.locator("#toggle-panel").textContent()) === "Show") await view.locator("#toggle-panel").click();
    await view.getByLabel(/Sparse infill/).click();
    await view.getByLabel(/^\s*Travel/).click();
    await page.waitForTimeout(2000);
    note("toggle", await viewState(view));
    await shot("toggle");
    await view.getByLabel(/Sparse infill/).click();
    await view.getByLabel(/^\s*Travel/).click();
    await page.waitForTimeout(1000);
  }
  if (steps.includes("pick")) {
    // Click the shown layer's outer wall where the view draws it.
    const frameBox = await (await view.frameElement()).boundingBox();
    const at = await view.evaluate(() => window.slicerView.screenOf(window.slicerView.state.layer, "external_perimeter"));
    if (at) await page.mouse.click(frameBox.x + at.x, frameBox.y + at.y);
    await page.waitForTimeout(1500);
    const stage = at;
    note("pick", { stage, ...(await viewState(view)) });
    await shot("pick");
  }
  if (steps.includes("hero")) {
    await heroOn(page);
    view = await findView(page, 1);
    note("hero", await viewState(view));
    await shot("hero");
  }
  console.log(JSON.stringify(out, null, 2));
}
