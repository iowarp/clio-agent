// AQ30 and AQ31: a view's live service carried through CLIO's route (MA23).
//
// Needs the service fixture (tests/fixtures/mcp_app_service_server.py) saved as a
// connection, with CLIO_SERVICE_FIXTURE_PORT and CLIO_SERVICE_FIXTURE_TOKEN set in
// its env and the same values exported here as SERVICE_PORT and SERVICE_TOKEN.
// SERVICE_PROMPT is the request (default: a plain request naming no tool).
//
// AQ30: opens the view, waits for its counter to tick over the WebSocket, bumps it
// over HTTP, and records every request and WebSocket of the page and its frames:
// each must go to CLIO's agent or UI address, none to the service port, and the
// token must appear in no URL, header, body or frame.
// AQ31: with a second view open, calls the first app's service route with the
// second app's capability (refused), closes the first app (its WebSocket closes
// with 4410 mcp_app_closed) and calls its route again (refused).
import { allowPending } from "./_lib.mjs";

const agent = new URL(process.env.AGENT_URL || `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`);
const servicePort = String(process.env.SERVICE_PORT || "19700");
const token = process.env.SERVICE_TOKEN || "";
const prompt = process.env.SERVICE_PROMPT || "Show me the live counter.";
const again = process.env.SERVICE_PROMPT_AGAIN || "Open another live counter view.";

const agentHosts = new Set([`127.0.0.1:${agent.port}`, `localhost:${agent.port}`]);
// A view runs on its own sandbox host, s<hash>.mcp-apps.localhost on CLIO's port: that is
// CLIO's own address too (browsers resolve *.localhost to loopback).
const isClio = (host) =>
  agentHosts.has(host) || (host.endsWith(".mcp-apps.localhost:" + agent.port) && /^s[0-9a-f]+\./.test(host));

function identity(src, appId) {
  const url = new URL(src);
  return { sid: url.pathname.split("/")[3], appId, dataRef: url.searchParams.get("data_ref") };
}

async function sendMessage(page, text) {
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 });
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

// Wait for a new app frame, allowing the fixture's tool for this conversation when
// CLIO asks (the session's permission rules apply to the call).
async function waitForFrame(page, frame) {
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline) {
    if (await frame.count()) return;
    const allow = page.getByRole("button", { name: /This session/ });
    if (await allow.count()) await allow.first().click().catch(() => undefined);
    // The card's button can be out of reach (a turn still streaming); allow it as
    // the person would, through the permissions route.
    const sid = new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/)?.[1];
    if (sid) await allowPending(decodeURIComponent(sid), []).catch(() => undefined);
    await page.waitForTimeout(1000);
  }
  throw new Error("no app frame appeared");
}

async function viewFrames(page, appId) {
  const outer = page.frameLocator(`[data-mcp-app-iframe="${appId}"]`);
  const view = outer.frameLocator("iframe");
  return { view, servicePage: view.frameLocator("#page") };
}

export default async function (page, shot) {
  const report = { requests: [], sockets: [], tokenSeen: [], servicePortSeen: [], foreign: [] };
  const check = (where, text) => {
    if (token && text && text.includes(token)) report.tokenSeen.push(where);
  };
  page.on("request", (request) => {
    const url = request.url();
    report.requests.push(`${request.method()} ${url}`);
    check(`request url ${url}`, url);
    check(`request headers ${url}`, JSON.stringify(request.headers()));
    check(`request body ${url}`, request.postData() || "");
    if (/^(data|blob|about):/.test(url)) return;
    const parsed = new URL(url);
    if (parsed.port === servicePort) report.servicePortSeen.push(url);
    if (!isClio(parsed.host) && parsed.origin !== new URL(page.url()).origin) {
      report.foreign.push(url);
    }
  });
  page.on("response", async (response) => {
    check(`response headers ${response.url()}`, JSON.stringify(response.headers()));
    const type = response.headers()["content-type"] || "";
    if (/json|html|javascript|text\/plain/.test(type) && !/event-stream/.test(type)) {
      check(`response body ${response.url()}`, await response.text().catch(() => ""));
    }
  });
  page.on("websocket", (socket) => {
    const url = socket.url();
    report.sockets.push(url);
    check(`websocket url ${url}`, url);
    const parsed = new URL(url);
    if (parsed.port === servicePort) report.servicePortSeen.push(url);
    if (!isClio(parsed.host)) report.foreign.push(url);
    socket.on("framesent", (frame) => check(`frame sent ${url}`, String(frame.payload)));
    socket.on("framereceived", (frame) => check(`frame received ${url}`, String(frame.payload)));
  });

  // AQ30: open the view and use it, in a new conversation.
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 });
  const fresh = page.getByText("New conversation", { exact: true }).filter({ visible: true }).first();
  await fresh.waitFor({ state: "visible", timeout: 180000 });
  await fresh.click();
  await page.waitForTimeout(1500);
  await sendMessage(page, prompt);
  const firstFrame = page.locator("[data-mcp-app-iframe]").first();
  await waitForFrame(page, firstFrame);
  const firstId = await firstFrame.getAttribute("data-mcp-app-iframe");
  const first = identity(await firstFrame.getAttribute("src"), firstId);
  const { view, servicePage } = await viewFrames(page, firstId);
  await view.locator("#socket", { hasText: "connected through CLIO" }).waitFor({ timeout: 60000 });
  const before = Number(await view.locator("#count").innerText());
  await page.waitForTimeout(3000);
  const after = Number(await view.locator("#count").innerText());
  await view.locator("#http", { hasText: "answered" }).waitFor({ timeout: 20000 });
  await view.getByRole("button", { name: "Bump the counter" }).click();
  await view.locator("#bump", { hasText: "accepted" }).waitFor({ timeout: 20000 });
  await servicePage
    .locator("#s", { hasText: "connected over its own WebSocket" })
    .waitFor({ timeout: 20000 });
  await firstFrame.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  await shot("service-view");
  report.view = {
    ticked: { before, after },
    socket: await view.locator("#socket").innerText(),
    http: await view.locator("#http").innerText(),
    bump: await view.locator("#bump").innerText(),
    servicePage: await servicePage.locator("body").innerText(),
  };

  // AQ30 is about the view in a tab: the fixture's view asks for full screen when it loads.
  report.placement = (await page.locator(`[data-mcp-app-tab] [data-mcp-app-iframe="${firstId}"]`).count())
    ? "tab"
    : "inline";
  report.frames = await page.locator("[data-mcp-app-iframe]").count();
  const { requests: _r, sockets: _s, ...aq30 } = report;
  console.log(`AQ30 ${JSON.stringify({ ...aq30, requestCount: _r.length, sockets: _s })}`);

  // AQ31: a socket held on the first app's route, from outside the page.
  const base = (app, cap) =>
    `${agent.origin}/v1/sessions/${app.sid}/mcp-apps/${app.appId}/service/${cap}`;
  const held = new WebSocket(base(first, first.dataRef).replace(/^http/, "ws") + "/ws");
  const heldClose = new Promise((resolve) => {
    held.addEventListener("close", (event) => resolve({ code: event.code, reason: event.reason }));
  });
  await new Promise((resolve, reject) => {
    held.addEventListener("message", resolve, { once: true });
    held.addEventListener("error", reject, { once: true });
  });

  await sendMessage(page, again);
  const secondFrame = page.locator(`[data-mcp-app-iframe]:not([data-mcp-app-iframe="${firstId}"])`).first();
  await waitForFrame(page, secondFrame);
  const secondId = await secondFrame.getAttribute("data-mcp-app-iframe");
  const second = identity(await secondFrame.getAttribute("src"), secondId);
  const crossed = await fetch(`${base(first, second.dataRef)}/state`);
  const crossedBack = await fetch(`${base(second, first.dataRef)}/state`);
  const own = await fetch(`${base(second, second.dataRef)}/state`);
  // Close the first app the way the UI does; the UI may already have closed it.
  const closed = await fetch(
    `${agent.origin}/v1/sessions/${first.sid}/mcp-apps/${first.appId}?data_ref=${first.dataRef}`,
    { method: "DELETE" },
  );
  const socketClose = await Promise.race([
    heldClose,
    new Promise((resolve) => setTimeout(() => resolve("still open"), 15000)),
  ]);
  const afterClose = await fetch(`${base(first, first.dataRef)}/state`);
  const reopened = await new Promise((resolve) => {
    const retry = new WebSocket(base(first, first.dataRef).replace(/^http/, "ws") + "/ws");
    retry.addEventListener("open", () => resolve("opened"));
    retry.addEventListener("error", () => resolve("refused"));
  });
  await secondFrame.scrollIntoViewIfNeeded();
  await page.waitForTimeout(2000);
  await shot("second-view");
  report.aq31 = {
    first: first.appId,
    second: second.appId,
    otherCapability: [crossed.status, crossedBack.status],
    ownCapability: own.status,
    closeStatus: closed.status,
    heldSocket: socketClose,
    afterCloseHttp: afterClose.status,
    afterCloseSocket: reopened,
  };
  report.session = first.sid;
  console.log(JSON.stringify(report, null, 2));
}
