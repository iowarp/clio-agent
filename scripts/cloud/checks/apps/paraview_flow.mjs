// A scripted ParaView conversation through CLIO's real UI (AQ38 to AQ48).
//
// PV_ACTIONS names a JSON file holding a list of actions, run in order:
//   {"open": NAME}             a new conversation in .cloud-run/apps-work/NAME
//   {"session": SID, "workspace": WID}   open an existing conversation
//   {"send": TEXT}             a plain request through the composer; waits for the turn,
//                              allowing the session's permission asks as a person would
//   {"view": true}             wait for the ParaView view (trame framed through CLIO's
//                              service route) and log its bar and status
//   {"status": LABEL}          log the view's bar and status now
//   {"until": REGEX, "seconds": N}   wait until the view's bar or status matches
//   {"shot": NAME}             screenshot
//   {"hero": NAME}             screenshot with the navigation sidebar collapsed and the
//                              workbench maximized, then put both back
//   {"maximize": true}         collapse the sidebar and maximize the workbench, and stay so
//   {"restore": true}          undo "maximize": restore the workbench and open the sidebar
//   {"wait": MS}
//   {"shell": CMD, "label": L} run a command on this machine and log its output
//   {"drag": true}             rotate the scene, counting the view's WebSocket frames
//   {"wheel": N}               scroll the mouse wheel N notches over the view (negative zooms in)
//   {"timeIndex": N}           move the view's own time slider to step N (0-based)
//   {"followLatest": true}     press the view's "Follow latest"
//   {"modeRemote": true} / {"modeLocal": true}   press the view's drawing toggle
//   {"modes": true}            log the drawing toggle's state and tooltips
//   {"click": true}            click (pick) a point left of the view's centre
//   {"reconnect": true}        press the view's Reconnect, if shown
//   {"closeTab": true}         close the app's tab
//   {"reopen": true}           press Reopen on the last app card
//   {"showTab": true}          press "Show tab" (live view) or "Reopen" (closed) on the last card
//   {"restart": true}          restart the stack, open the conversation again, open the
//                              workspace canvas and focus the restored app tab
// The view's ParaView panel (the person's controls):
//   {"panel": LABEL}           press a tool rail icon (Pipeline, Add filter, Display,
//                              Camera, Information); pressing the open one closes it
//   {"visible": NAME, "on": BOOL}   show or hide a pipeline object with its eye
//   {"closePanel": true}       close the open panel; the rail stays
//   {"row": TEXT}              select the pipeline row whose name contains TEXT
//   {"filter": NAME, "axis": "X|Y|Z", "values": {LABEL: VALUE}}   Add filter: choose the
//                              filter for the selected object, set the normal and fields, Apply
//   {"choose": LABEL, "item": TEXT}   pick TEXT in the panel's LABEL select
//   {"press": LABEL}           press a button of the ParaView page by its accessible name
//   {"panelText": LABEL}       log the open panel's text and the pipeline rows
//   {"theme": "dark"|"light"}  switch CLIO's theme as its own switch does
//   {"stepMenu": NAME}         open the step n/N menu (previous, slider, next), shoot NAME,
//                              close it, and log the bar's height (one row is about 36 px)
//   {"play": NAME}             press Play, sample the step four times, shoot NAME, press Pause
// Every request and WebSocket the page opens is recorded. PV_OUT (a file) receives the
// log, the requests, the sockets and the turns' answers as JSON, for the token search (AQ49).
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { AGENT, allowPending, api, openConversation, send } from "./_lib.mjs";

const actions = JSON.parse(readFileSync(process.env.PV_ACTIONS, "utf8"));
const out = { log: [], requests: [], sockets: [], foreign: [], answers: [], frames: [] };
const agentHost = new URL(AGENT).host;
// CLIO's own address: the agent, and its per-app sandbox origins on the same port.
const isClio = (host) => host === agentHost || host.endsWith(`.mcp-apps.localhost:${new URL(AGENT).port}`);

function note(label, value) {
  out.log.push({ at: new Date().toISOString(), label, value });
  console.error(`[pv] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
}

function sessionId(page) {
  const match = new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

/** Wait for the session's turn to end, allowing its permission asks for the session. */
async function settle(page, minutes = 15) {
  const sid = sessionId(page);
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
  const listed = await api("GET", `/v1/sessions/${sid}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || [];
  const latest = rows.find((m) => m.role === "assistant") || rows.at(-1);
  const parts = latest?.parts || [];
  const calls = parts.filter((p) => p.type === "tool_call").map((p) => `${p.tool_name} ${JSON.stringify(p.input ?? {})}`);
  const text = parts.filter((p) => p.type === "text").map((p) => p.text).join("\n");
  out.answers.push({ session: sid, calls, text });
  note("turn", { calls, text: text.slice(0, 1500) });
}

/** The view's own page (view.html) and the trame page it frames. */
function frames(page) {
  const trame = page.frames().find((f) => f.url().includes("/service/"));
  const view = trame ? trame.parentFrame() : null;
  return { view, trame };
}

async function viewText(page) {
  const { view, trame } = frames(page);
  const status = view
    ? await view.evaluate(() => {
        const box = document.getElementById("status");
        return box && !box.hidden ? box.textContent : "";
      }).catch(() => "")
    : "";
  const bar = trame ? await trame.locator(".clio-bar").innerText({ timeout: 3000 }).catch(() => "") : "";
  return { status, bar: bar.replace(/\s+/g, " ").trim(), framed: Boolean(trame) };
}

async function waitFor(page, check, seconds, label) {
  const deadline = Date.now() + seconds * 1000;
  const allowed = [];
  while (Date.now() < deadline) {
    const sid = sessionId(page);
    if (sid) await allowPending(sid, allowed).catch(() => undefined);
    if (await check().catch(() => false)) return true;
    await page.waitForTimeout(1000);
  }
  note("timed out", label);
  return false;
}

async function waitHttp(url, timeout = 600000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fetch(url).then((r) => r.ok).catch(() => false)) return;
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  throw new Error(`${url} did not answer`);
}

async function allowOnce(page, timeout = 20000) {
  const button = page.getByRole("button", { name: /Allow once/ });
  if (await button.first().waitFor({ timeout }).then(() => true, () => false)) await button.first().click();
}

async function canvasBox(page) {
  const { trame } = frames(page);
  for (const canvas of await trame.locator(".clio-view canvas").elementHandles()) {
    const box = await canvas.boundingBox();
    if (box && box.width > 0 && box.x > -1000) return box;
  }
  return null;
}

/** Press one of the view's two drawing buttons; log both buttons' state and tooltips. */
async function pressMode(page, label) {
  const { trame } = frames(page);
  await trame.locator(`[aria-label="Drawing"] button[aria-label="${label}"]`).click();
  await page.waitForTimeout(5000);
  const buttons = await trame.locator('[aria-label="Drawing"] button').evaluateAll((all) =>
    all.map((b) => ({ label: b.getAttribute("aria-label"), pressed: b.getAttribute("aria-pressed"), title: b.title })),
  );
  note(`drawing ${label}`, { buttons, ...(await viewText(page)) });
}

async function waitView(page) {
  const framed = await waitFor(page, async () => Boolean(frames(page).trame), 600, "the ParaView view");
  if (!framed) return note("view", { framed: false, ...(await viewText(page)) });
  // Moving the view between card and tab reloads its frame: look again until it stays.
  await waitFor(page, async () => {
    const { trame } = frames(page);
    if (!trame) return false;
    await trame.locator(".clio-view canvas").first().waitFor({ timeout: 5000 });
    return true;
  }, 180, "the view's canvas");
  await page.waitForTimeout(4000);
  note("view", await viewText(page));
}

async function panelText(page) {
  const { trame } = frames(page);
  const panel = await trame.locator(".clio-panel").innerText({ timeout: 3000 }).catch(() => "");
  const rows = await trame.locator(".clio-row").evaluateAll((all) =>
    all.map((r) => ({
      name: r.innerText.trim(),
      current: r.querySelector("[aria-current]")?.getAttribute("aria-current"),
      eye: r.querySelector("button")?.getAttribute("aria-label"),
    })),
  ).catch(() => []);
  const error = await trame.locator('.clio-panel [role="alert"]').innerText({ timeout: 500 }).catch(() => "");
  return { panel: panel.replace(/\s+/g, " ").trim().slice(0, 600), rows, error };
}

const run = {
  async open(page, name) {
    const { session, workspace } = await openConversation(page, name);
    note("conversation", { session: session.id, workspace: workspace.id });
  },
  async session(page, sid, action) {
    await page.goto(new URL(`/workspaces/${action.workspace}/sessions/${sid}`, page.url()).toString());
    await page.waitForTimeout(6000);
  },
  async send(page, text) {
    note("send", text);
    await send(page, text);
    await page.waitForTimeout(4000);
    await settle(page);
  },
  view: (page) => waitView(page),
  async status(page, label) {
    note(`status ${label}`, await viewText(page));
  },
  async until(page, pattern, action) {
    const re = new RegExp(pattern);
    await waitFor(page, async () => {
      const t = await viewText(page);
      return re.test(`${t.status} ${t.bar}`);
    }, action.seconds || 120, pattern);
    note(`until ${pattern}`, await viewText(page));
  },
  shot: (page, name, action, shot) => shot(name),
  async maximize(page) {
    const toggle = page.locator('[data-slot="sidebar-trigger"]').first();
    const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
    if (await toggle.isVisible().catch(() => false)) await toggle.click();
    if (await maximize.isVisible().catch(() => false)) await maximize.click();
    await page.waitForTimeout(2000);
    await waitFor(page, async () => Boolean(frames(page).trame), 120, "the view after maximizing");
    await page.waitForTimeout(8000);
    note("maximized", await viewText(page));
  },
  async restore(page) {
    const restore = page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).first();
    if (await restore.isVisible().catch(() => false)) await restore.click();
    const toggle = page.locator('[data-slot="sidebar-trigger"]').first();
    if (await toggle.isVisible().catch(() => false)) await toggle.click();
    await page.waitForTimeout(3000);
    note("restored", await viewText(page));
  },
  async hero(page, name, action, shot) {
    const toggle = page.locator('[data-slot="sidebar-trigger"]').first();
    const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
    if (await toggle.isVisible().catch(() => false)) await toggle.click();
    if (await maximize.isVisible().catch(() => false)) await maximize.click();
    // Maximizing remounts the workbench, so the view reloads its frame: wait for it.
    await page.waitForTimeout(2000);
    await waitFor(page, async () => Boolean(frames(page).trame), 120, "the view after maximizing");
    await frames(page).trame?.locator(".clio-view canvas").first().waitFor({ timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(8000);
    await shot(name);
    const restore = page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).first();
    if (await restore.isVisible().catch(() => false)) await restore.click();
    if (await toggle.isVisible().catch(() => false)) await toggle.click();
    await page.waitForTimeout(2000);
  },
  wait: (page, ms) => page.waitForTimeout(ms),
  async shell(page, cmd, action) {
    let text;
    try {
      text = execSync(cmd, { encoding: "utf8", shell: "/bin/bash", timeout: 300000 });
    } catch (error) {
      text = `exit ${error.status}\n${error.stdout || ""}${error.stderr || ""}`;
    }
    note(action.label || cmd, text.trim());
  },
  async drag(page) {
    const box = await canvasBox(page);
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
    const during = out.frames.filter((f) => f.at >= start && f.at <= Date.now() + 150);
    note("drag", { frames: during.length, imageFrames: during.filter((f) => f.binary && f.size > 2000).length });
    await page.waitForTimeout(2000);
  },
  async wheel(page, notches) {
    const box = await canvasBox(page);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    for (let i = 0; i < Math.abs(notches); i += 1) {
      await page.mouse.wheel(0, Math.sign(notches) * 120);
      await page.waitForTimeout(120);
    }
    // The browser reports its camera after the interaction ends.
    await page.mouse.down();
    await page.mouse.up();
    await page.waitForTimeout(2000);
    note("wheel", notches);
  },
  async timeIndex(page, index) {
    const { trame } = frames(page);
    await trame.locator('.clio-bar input[type=range]').evaluate((el, value) => {
      el.value = String(value);
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }, index);
    await page.waitForTimeout(3000);
    note("time slider", { index, ...(await viewText(page)) });
  },
  async followLatest(page) {
    await frames(page).trame.getByRole("button", { name: "Follow latest" }).click();
    await page.waitForTimeout(3000);
    note("follow latest", await viewText(page));
  },
  async modes(page) {
    const { trame } = frames(page);
    note("drawing", await trame.locator('[aria-label="Drawing"] button').evaluateAll((all) =>
      all.map((b) => ({ label: b.getAttribute("aria-label"), pressed: b.getAttribute("aria-pressed"), title: b.title })),
    ));
  },
  async click(page) {
    const box = await canvasBox(page);
    await page.mouse.click(box.x + box.width * 0.42, box.y + box.height / 2);
    await page.waitForTimeout(3000);
    note("clicked", await viewText(page));
  },
  modeRemote: (page) => pressMode(page, "Render on ParaView machine"),
  modeLocal: (page) => pressMode(page, "Draw in browser"),
  async reconnect(page) {
    const { view } = frames(page);
    const button = view?.getByRole("button", { name: "Reconnect" });
    if (button && (await button.count())) await button.click();
    await page.waitForTimeout(5000);
    note("reconnect", await viewText(page));
  },
  async closeTab(page) {
    const tab = page.locator("[data-mcp-app-tab]").first();
    const id = await tab.getAttribute("data-mcp-app-tab");
    const chip = page.locator(`[data-tab-id="mcp-app:${id}"]`);
    await chip.hover();
    await chip.locator('[data-slot="frame-tab-close"]').click({ force: true });
    await tab.waitFor({ state: "detached", timeout: 30000 });
    note("tab closed", id);
    await page.waitForTimeout(2000);
  },
  async reopen(page) {
    for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
      await group.click().catch(() => undefined);
    const lines = page.locator("[data-mcp-app-reopen]");
    await lines.last().waitFor({ timeout: 60000 });
    await lines.last().scrollIntoViewIfNeeded();
    await lines.last().getByRole("button", { name: "Reopen" }).click();
    await allowOnce(page);
    note("reopened", await lines.last().getAttribute("data-mcp-app-reopen"));
  },
  async showTab(page) {
    // A live view's card offers "Show tab"; a closed one offers "Reopen" (its saved call).
    const button = page.getByRole("button", { name: /^(Show tab|Reopen)$/ }).last();
    await button.waitFor({ timeout: 60000 });
    await button.scrollIntoViewIfNeeded();
    const label = (await button.innerText()).trim();
    await button.click();
    await allowOnce(page, 5000);
    note("card", `pressed ${label}`);
  },
  async panel(page, label) {
    const open = await frames(page).trame.locator(".clio-panel header").innerText({ timeout: 500 }).catch(() => "");
    // Open the panel; when it is already open, leave it (closePanel closes it).
    if (open.trim() !== label) await frames(page).trame.locator(`.clio-rail button[aria-label="${label}"]`).click();
    await page.mouse.move(page.viewportSize().width - 60, page.viewportSize().height - 30); // no tooltip in the shots
    await page.waitForTimeout(1200);
    note(`panel ${label}`, await panelText(page));
  },
  async visible(page, name, action) {
    // Show or hide a pipeline object with its eye, only when it is not already so.
    const { trame } = frames(page);
    const eye = trame.locator(`.clio-row button[aria-label="${action.on ? "Show" : "Hide"} ${name}"]`);
    if (await eye.count()) await eye.first().click();
    await page.waitForTimeout(2000);
    note(`visible ${name} ${action.on}`, await panelText(page));
  },
  async closePanel(page) {
    await frames(page).trame.locator('.clio-panel button[aria-label="Close panel"]').click();
    await page.waitForTimeout(1500);
    note("panel closed", await panelText(page));
  },
  async row(page, text) {
    const { trame } = frames(page);
    if (!(await trame.locator(".clio-row").count()))
      await trame.locator('.clio-rail button[aria-label="Pipeline"]').click();
    await trame.locator(".clio-row .clio-name", { hasText: text }).first().click();
    await page.waitForTimeout(1500);
    note(`row ${text}`, await panelText(page));
  },
  async filter(page, name, action) {
    const { trame } = frames(page);
    if (!(await trame.locator('.clio-panel [aria-label="Filters"]').count()))
      await trame.locator('.clio-rail button[aria-label="Add filter"]').click();
    await trame.locator(`[aria-label="Filters"] button[aria-label^="${name}"]`).click();
    await page.waitForTimeout(1200);
    if (action.axis) await trame.locator(`button[aria-label="Normal along ${action.axis}"]`).click();
    for (const [label, value] of Object.entries(action.values || {})) {
      const field = trame.locator(".clio-form .v-text-field", { hasText: label }).locator("input").first();
      await field.fill(String(value));
    }
    await trame.getByRole("button", { name: "Apply" }).click();
    await page.waitForTimeout(5000);
    note(`filter ${name}`, await panelText(page));
  },
  async choose(page, label, action) {
    const { trame } = frames(page);
    if (!(await trame.locator(".clio-panel .v-select", { hasText: label }).count()))
      await trame.locator('.clio-rail button[aria-label="Display"]').click();
    await trame.locator(".clio-panel .v-select", { hasText: label }).first().click();
    await page.waitForTimeout(700);
    await trame.locator(".v-overlay .v-list-item", { hasText: action.item }).first().click();
    await page.waitForTimeout(4000);
    note(`choose ${label}`, { item: action.item, ...(await panelText(page)) });
  },
  async press(page, label) {
    await frames(page).trame.locator(`button[aria-label="${label}"]`).first().click();
    await page.mouse.move(page.viewportSize().width - 60, page.viewportSize().height - 30);
    await page.waitForTimeout(3000);
    note(`press ${label}`, await panelText(page));
  },
  async stepMenu(page, name, action, shot) {
    const { trame } = frames(page);
    const bar = await trame.locator(".clio-bar").evaluate((b) => Math.round(b.getBoundingClientRect().height));
    await trame.locator(".clio-bar .clio-step").click();
    await page.waitForTimeout(1000);
    const menu = await trame.locator(".clio-step-menu").evaluate((m) =>
      [...m.querySelectorAll("button, input")].map((e) => e.getAttribute("aria-label")),
    ).catch(() => []);
    await shot(name);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(600);
    note("step menu", { barHeight: bar, menu, ...(await viewText(page)) });
  },
  async play(page, name, action, shot) {
    const { trame } = frames(page);
    await trame.locator('button[aria-label="Play"]').click();
    const samples = [];
    for (let i = 0; i < 4; i++) {
      await page.waitForTimeout(400);
      samples.push(await trame.evaluate(() => [window.trame.state.get("clio_index"), window.trame.state.get("clio_playing")]));
    }
    await shot(name);
    const pause = trame.locator('button[aria-label="Pause"]');
    const paused = (await pause.count()) ? (await pause.click(), true) : false;
    await page.waitForTimeout(1500);
    note("play", { samples, paused, ...(await viewText(page)) });
  },
  async panelText(page, label) {
    note(`panel text ${label}`, await panelText(page));
  },
  async theme(page, name) {
    // What CLIO's own theme switch does to <html> (theme/preference.ts switchTheme).
    await page.evaluate((dark) => {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      document.documentElement.classList.toggle("dark", dark);
    }, name === "dark");
    await page.waitForTimeout(4000);
    const { trame } = frames(page);
    const look = await trame.evaluate(() => {
      const app = document.querySelector(".v-application");
      const style = app ? getComputedStyle(app) : null;
      return { background: style?.backgroundColor, color: style?.color, view: window.trame.state.get("clio_view_colors") };
    }).catch(() => null);
    note(`theme ${name}`, look);
  },
  async restart(page) {
    const sessionUrl = page.url();
    await page.waitForTimeout(1500); // the workbench saves its tabs on change
    await page.goto("about:blank");
    const started = Date.now();
    execSync("bash scripts/cloud/stack.sh restart", { stdio: "ignore" });
    await waitHttp(`${AGENT}/v1/health`);
    await waitHttp(`${AGENT}/v1/workspaces`);
    await waitHttp(new URL("/", sessionUrl).toString());
    note("restarted", { seconds: Math.round((Date.now() - started) / 1000) });
    await page.waitForTimeout(5000);
    await page.goto(sessionUrl);
    await page.waitForTimeout(6000);
    const canvas = page.getByRole("button", { name: "Open workspace canvas" });
    if (await canvas.count()) await canvas.first().click();
    const restored = page.locator('[data-tab-id^="mcp-app:"]').first();
    if (await restored.waitFor({ timeout: 30000 }).then(() => true, () => false)) {
      note("restored tab", await restored.getAttribute("data-tab-id"));
      await restored.click();
      await allowOnce(page);
    } else {
      note("restored tab", "none: the view comes back from its card");
      await run.showTab(page);
    }
  },
};

export default async function (page, shot) {
  page.on("request", (request) => {
    const url = request.url();
    out.requests.push({ method: request.method(), url, headers: request.headers() });
    if (/^(data|blob|about):/.test(url)) return;
    const parsed = new URL(url);
    if (!isClio(parsed.host) && parsed.origin !== new URL(page.url()).origin) out.foreign.push(url);
  });
  page.on("pageerror", (error) =>
    note("page error", { name: error?.name, message: error?.message, stack: String(error?.stack || "").slice(0, 800) }),
  );
  page.on("websocket", (socket) => {
    const url = socket.url();
    out.sockets.push(url);
    if (!isClio(new URL(url).host)) out.foreign.push(url);
    if (!url.includes("/service/")) return;
    socket.on("framereceived", (f) =>
      out.frames.push({ at: Date.now(), size: f.payload.length, binary: typeof f.payload !== "string" }),
    );
    socket.on("close", () => note("service socket closed", url.replace(/service\/[^/]+/, "service/…")));
  });
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 }).catch(() => undefined);
  try {
    for (const action of actions) {
      const [name] = Object.keys(action);
      await run[name](page, action[name], action, shot);
    }
  } finally {
    note("session", sessionId(page));
    if (process.env.PV_OUT) {
      const frames = out.frames.length;
      writeFileSync(process.env.PV_OUT, JSON.stringify({ ...out, frames }, null, 1));
    }
  }
}
