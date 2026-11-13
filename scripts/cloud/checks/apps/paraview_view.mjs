// The ParaView connector's live view through CLIO (AQ36, AQ37, AQ45, AQ49).
//
// Needs the paraview server reachable as a CLIO connection or connector (the
// `clio-kit mcp-server paraview` command) and Chromium with WebGL (ui_check asks
// for software WebGL itself on a machine with no GPU). Environment:
//   PV_PROMPT     the opening request (default: a plain request naming no tool)
//   PV_DATASET    dataset path on the machine where ParaView runs
//   PV_FIELD      the field for the isosurface
//   PV_ASK        the follow-up question (default: range and pick)
//   PV_EXPECT_MODE  "local" (default) or "remote" (AQ45, lowered threshold)
//
// It opens the view, waits for the agent's turn to end, drags the scene while
// recording the view's WebSocket frames (local mode: no image frames during the
// drag), clicks a point, asks the follow-up, and records every request: each
// must go to CLIO's agent or UI address, none to the node half's port.
const agent = new URL(process.env.AGENT_URL || `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`);
const dataset = process.env.PV_DATASET || "/data/wavelet.vti";
const field = process.env.PV_FIELD || "RTData";
const prompt =
  process.env.PV_PROMPT || `Open ${dataset} in ParaView and show me an isosurface of ${field}.`;
const ask = process.env.PV_ASK || `What is the range of ${field}, and what did I pick in the view?`;
const expectMode = process.env.PV_EXPECT_MODE || "local";
const agentHosts = new Set([`127.0.0.1:${agent.port}`, `localhost:${agent.port}`]);

async function sendMessage(page, text) {
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

// Allow the paraview tools for this conversation when CLIO asks, until `done()`.
async function allowUntil(page, done, timeout = 600000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await done()) return true;
    const allow = page.getByRole("button", { name: /This session/ });
    if (await allow.count()) await allow.first().click().catch(() => undefined);
    await page.waitForTimeout(1000);
  }
  return false;
}

async function turnEnded(page) {
  const stop = page.getByRole("button", { name: /^Stop/ });
  return (await stop.count()) === 0 || !(await stop.first().isVisible().catch(() => false));
}

async function settle(page, timeout = 600000) {
  let quiet = 0;
  await allowUntil(
    page,
    async () => {
      quiet = (await turnEnded(page)) ? quiet + 1 : 0;
      return quiet >= 5;
    },
    timeout,
  );
}

async function visibleCanvas(frame) {
  for (const canvas of await frame.locator(".clio-view canvas").elementHandles()) {
    const box = await canvas.boundingBox();
    if (box && box.width > 0 && box.x > -1000) return box;
  }
  return null;
}

export default async function (page, shot) {
  const report = { requests: [], sockets: [], foreign: [], frames: [], prompt, ask };
  page.on("request", (request) => {
    const url = request.url();
    report.requests.push(`${request.method()} ${url}`);
    if (/^(data|blob|about):/.test(url)) return;
    const parsed = new URL(url);
    if (!agentHosts.has(parsed.host) && parsed.origin !== new URL(page.url()).origin) {
      report.foreign.push(url);
    }
  });
  page.on("websocket", (socket) => {
    const url = socket.url();
    report.sockets.push(url);
    if (!agentHosts.has(new URL(url).host)) report.foreign.push(url);
    if (!url.includes("/service/")) return;
    socket.on("framereceived", (f) =>
      report.frames.push({ at: Date.now(), size: f.payload.length, binary: typeof f.payload !== "string" }),
    );
  });

  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 });
  const fresh = page.getByText("New conversation", { exact: true }).filter({ visible: true }).first();
  await fresh.waitFor({ state: "visible", timeout: 180000 });
  await fresh.click();
  await page.waitForTimeout(1500);
  await sendMessage(page, prompt);

  const app = page.locator("[data-mcp-app-iframe]").first();
  if (!(await allowUntil(page, async () => (await app.count()) > 0))) {
    await shot("no-view");
    throw new Error("no ParaView view appeared");
  }
  const appId = await app.getAttribute("data-mcp-app-iframe");
  report.session = new URL(await app.getAttribute("src")).pathname.split("/")[3];
  const viewFrame = () =>
    page.frames().find((f) => f.url().includes("/service/") && f.url().includes("index.html"));
  await allowUntil(page, async () => Boolean(viewFrame()), 240000);
  await settle(page);
  const trame = viewFrame();
  if (!trame) {
    await shot("no-trame");
    throw new Error("the view never framed the ParaView service");
  }
  await trame.locator(".clio-view canvas").first().waitFor({ timeout: 120000 });
  await page.waitForTimeout(4000);
  await shot("view-open");
  report.viewBar = await trame.locator(".clio-bar").innerText();

  // AQ36: rotate. In local mode the browser draws; no image arrives mid-drag.
  const box = await visibleCanvas(trame);
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  const start = Date.now();
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  for (let i = 1; i <= 30; i++) {
    await page.mouse.move(cx + i * 6, cy + i * 2);
    await page.waitForTimeout(30);
  }
  await page.mouse.up();
  const end = Date.now();
  const during = report.frames.filter((f) => f.at >= start && f.at <= end + 150);
  report.drag = {
    frames: during.length,
    imageFrames: during.filter((f) => f.binary && f.size > 2000).length,
    expectMode,
  };
  await page.waitForTimeout(2000);
  await shot("rotated");

  // AQ37: pick a point, then ask.
  await page.mouse.click(cx - box.width * 0.08, cy);
  await page.waitForTimeout(3000);
  await shot("picked");
  await sendMessage(page, ask);
  await page.waitForTimeout(3000);
  await settle(page);
  await shot("answer");
  const messages = page.locator('[data-message-role="assistant"], [data-role="assistant"]');
  report.answer = (await messages.count()) ? await messages.last().innerText() : null;
  report.appId = appId;
  console.log(JSON.stringify({ ...report, requests: report.requests.length, frames: report.frames.length }, null, 2));
}
