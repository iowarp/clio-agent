// An NDP study from a plain request (NDP ledger qualification), based on science_study.mjs.
//
// Differences: before typing the request it does what a person does on a fresh agent,
// clicking "Enable all" on the agent-needs card (the tool servers the agent's plugins
// declare) and waiting for it to finish; the Science log capture is optional; and it
// prints the loaded skills, every call with its outcome, and the final reply.
//
// STUDY_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS); STUDY_TEXT the
// request (a plain one, naming no tools or steps); STUDY_AGENT the agent (empty: CLIO's default). Plugin
// offers the agent makes are accepted as a person would (acceptOffers). The
// step opens a conversation there, sends the request, allows each question for the
// session, waits for the turn to finish, then opens the Science log from the canvas menu
// and captures its list and, when the study has views, its Views mode. It prints the
// calls the agent made, the study's entries by type and its views.
//
// Multi-turn: STUDY_FOLLOWUPS (a JSON list of plain requests) is sent turn by turn in
// the same conversation after the first one settles, each captured ("followup-N").
// STUDY_WORKSPACE and STUDY_SESSION continue an existing conversation instead of
// starting one (STUDY_TEXT may then be empty). STUDY_MINUTES bounds each turn's wait
// (default 60).
import path from "node:path";

const AGENT = process.env.AGENT_URL || "http://127.0.0.1:8787";

async function api(method, route, body) {
  const response = await fetch(AGENT + route, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

async function allowPending(sessionId, log) {
  const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
  for (const row of listed.permissions || listed.items || []) {
    const id = row.id || row.permission_id;
    if (!id) continue;
    log.push(`allowed: ${row.summary || row.title || row.tool_name || id}`);
    await api("POST", `/v1/permissions/${id}`, { action: "allow_session" });
  }
}

// A person accepts what the agent offers: a plugin offer (intent add_plugin) or a
// connector offer (intent enable) is answered with the card's own submit action
// (Add), then Enable all is clicked when the added servers ask to be turned on. A
// card asking for values (keys, settings) is left for a person. Each acceptance is
// logged with the plugin and the agent's reason, so the study shows which plugins
// the agent chose to load.
async function acceptOffers(page, sessionId, log) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/interactions`);
  for (const card of listed.interactions || []) {
    const payload = card.payload || {};
    if (card.status !== "pending" || !["add_plugin", "enable"].includes(payload.intent)) continue;
    if ((payload.missing || []).length) continue;
    await api("POST", `/v1/sessions/${sessionId}/interactions/${card.id}/respond`, { action: "submit" });
    log.push(
      `accepted ${payload.intent} offer ${payload.plugin_id || payload.connector_id || payload.name || card.title}: ${payload.reason || ""}`,
    );
  }
  const enable = page.getByRole("button", { name: /Enable all/i });
  if (await enable.count()) {
    await enable.first().click().catch(() => undefined);
    log.push("clicked Enable all for an added plugin's tool servers");
  }
}

async function waitIdle(page, sessionId, log, minutes) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sessionId, log);
    await acceptOffers(page, sessionId, log);
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(4000);
  }
  return false;
}

async function send(page, text) {
  const composer = page
    .locator('form:has(button[type="submit"]) textarea, form:has(button[type="submit"]) [contenteditable="true"]')
    .last();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

export default async function (page, shot) {
  const root = process.env.STUDY_ROOT;
  const text = process.env.STUDY_TEXT;
  const log = [];
  const minutes = Number(process.env.STUDY_MINUTES || 60);
  const followups = JSON.parse(process.env.STUDY_FOLLOWUPS || "[]");
  const continuing = Boolean(process.env.STUDY_SESSION);
  // STUDY_WORKSPACE alone opens a new conversation in that existing workspace.
  const workspace = process.env.STUDY_WORKSPACE
    ? { id: process.env.STUDY_WORKSPACE }
    : await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  // No STUDY_AGENT: the session gets CLIO's default agent, as a new conversation does.
  const session = continuing
    ? { id: process.env.STUDY_SESSION }
    : await api("POST", "/v1/sessions", {
        workspace_id: workspace.id,
        ...(process.env.STUDY_AGENT ? { agent: { id: process.env.STUDY_AGENT } } : {}),
      });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(6000);
  const enable = page.getByRole("button", { name: /Enable all/i });
  if (!continuing && (await enable.count())) {
    await enable.first().click();
    log.push("clicked Enable all on the agent-needs card");
    // The card's own phase (ask, working, done), read from the interactions API.
    const until = Date.now() + 15 * 60_000;
    while (Date.now() < until) {
      const listed = await api("GET", `/v1/sessions/${session.id}/interactions?include_recent_resolved=true`);
      const card = (listed.interactions || []).find((i) => (i.payload || {}).intent === "agent_needs");
      const phase = card ? card.payload.phase : "done";
      if (phase === "done") {
        log.push(`enable: ${(card ? card.payload.items : []).map((i) => `${i.id}=${i.progress}`).join(", ")}`);
        break;
      }
      await page.waitForTimeout(3000);
    }
    await shot("enabled");
  } else log.push("no agent-needs card");
  if (text) {
    await send(page, text);
    log.push(`turn settled: ${await waitIdle(page, session.id, log, minutes)}`);
    await shot("reply");
  }
  for (const [index, followup] of followups.entries()) {
    await send(page, followup);
    log.push(`follow-up ${index + 1} settled: ${await waitIdle(page, session.id, log, minutes)}`);
    await shot(`followup-${index + 1}`);
  }

  const study = await api("GET", `/v1/sessions/${session.id}/science-log`);
  const openCanvas = page.getByRole("button", { name: "Open workspace canvas" });
  const canvasMenu = page.getByTitle("Open a canvas tab");
  if (await openCanvas.count()) await openCanvas.first().click();
  await page.waitForTimeout(1500);
  if (await canvasMenu.count()) await canvasMenu.first().click({ timeout: 5000 }).catch(() => undefined);
  const item = page.getByRole("menuitem", { name: "Science log" });
  if (await item.count()) {
    await item.click();
    await page.waitForTimeout(4000);
    await shot("science-log");
    const views = page.getByRole("radio", { name: "Views" });
    if (await views.count()) {
      await views.first().click();
      await page.waitForTimeout(6000);
      await shot("science-log-views");
    }
  } else {
    log.push("no Science log in the canvas menu");
    await page.keyboard.press("Escape");
  }
  const listed = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  const calls = rows.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_call"));
  const entries = {};
  for (const entry of study.entries || []) entries[entry.type] = (entries[entry.type] || 0) + 1;
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspace.id,
        calls: calls.map((p) => p.tool_name),
        failed: rows.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_result" && p.is_error)).length,
        reply: rows.filter((m) => m.role === "assistant").flatMap((m) => (m.parts || []).filter((p) => p.type === "text")).map((p) => p.text).slice(-1)[0],
        skills: calls.filter((p) => p.tool_name === "load_skill").map((p) => (p.input || {}).skill_id || (p.input || {}).name),
        entries,
        views: (study.views || []).map((v) => ({ title: v.title, kind: v.kind, dashboard: v.dashboard_id })),
        log,
      },
      null,
      2,
    ),
  );
}
