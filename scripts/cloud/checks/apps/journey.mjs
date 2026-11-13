// First-run journeys (ledger "First-run journey"): a person types plain requests and
// answers what CLIO asks, as a person would, while this step file counts what they meet.
//
//   JOURNEY_PROMPTS   JSON array of plain requests, sent one after another (required)
//   JOURNEY_START     "landing" (default): type in the first screen's composer, as a new
//                     person does; "workspace:<name>": a new conversation in a workspace
//                     folder under .cloud-run/apps-work/<name>; "session:<path>": reopen
//                     an existing conversation by its UI path (/workspaces/<w>/sessions/<s>)
//   JOURNEY_PERMISSION "session" (default) clicks "Allow for this session"; "once" clicks
//                     "Allow once"
//   JOURNEY_SETUP     what to do with a connector setup card: "continue" (default) or
//                     "decline"
//   JOURNEY_TAB       "1": choose "Open in tab" on the newest app card after each turn
//   JOURNEY_HERO      "1": a hero shot (left navigation collapsed, canvas maximized) at the end
//   JOURNEY_MINUTES   the wait for each turn (default 10)
//
// Every permission card and setup card is screenshotted before it is answered, and its
// visible text is recorded. The record (one JSON line, also written to
// .cloud-run/journey/<label>.json) lists per request: seconds, prompts and their text, the
// tool calls, app frames and the answer's text.
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { ROOT, api, appFrames, heroShot, openConversation } from "./_lib.mjs";

const label = process.env.JOURNEY_LABEL || "journey";
const prompts = JSON.parse(process.env.JOURNEY_PROMPTS || "[]");
const start = process.env.JOURNEY_START || "landing";
const scope = process.env.JOURNEY_PERMISSION === "once" ? "Allow once" : "Allow for this session";
const setupAction = process.env.JOURNEY_SETUP || "continue";
const minutes = Number(process.env.JOURNEY_MINUTES || 10);

async function messages(sid) {
  const listed = await api("GET", `/v1/sessions/${sid}/messages`);
  return Array.isArray(listed) ? listed : listed.messages || listed.items || [];
}

function partText(part) {
  if (part.type === "text") return part.text || "";
  return "";
}

async function turnRecord(sid, seen) {
  const rows = (await messages(sid)).filter((m) => !seen.has(m.id));
  const tools = [];
  const answer = [];
  for (const message of rows) {
    for (const part of message.parts || []) {
      if (part.type === "tool_call") {
        const status = part.status || part.state?.status || "";
        tools.push(`${part.tool_name}:${status}`);
      } else if (part.type === "mcp_app") tools.push(`app:${part.source_server}/${part.tool_name}`);
      else if (message.role === "assistant") answer.push(partText(part));
    }
  }
  return { tools, answer: answer.join("\n").trim().slice(0, 2400), count: rows.length };
}

async function composerSend(page, text) {
  const composer = page.locator('[data-slot="clio-composer"] [contenteditable="true"], textarea').first();
  await composer.waitFor({ state: "visible", timeout: 120000 });
  const posted = page.waitForRequest(
    (r) => r.method() === "POST" && /\/v1\/sessions\/[^/]+\/messages$/u.test(r.url()),
    { timeout: 120000 },
  );
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
  return new URL((await posted).url()).pathname.split("/")[3];
}

export default async function (page, shot) {
  if (!prompts.length) throw new Error("JOURNEY_PROMPTS is required");
  if (start.startsWith("workspace:")) await openConversation(page, start.slice(10));
  else if (start.startsWith("session:")) {
    await page.goto(new URL(start.slice(8), page.url()).toString(), { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(6000);
  }
  await shot("start");
  const record = { label, start, turns: [] };
  let sid = "";
  let seen = new Set();
  for (const [index, text] of prompts.entries()) {
    const began = Date.now();
    if (sid) seen = new Set((await messages(sid)).map((m) => m.id));
    sid = await composerSend(page, text);
    const turn = { request: text, sid, permissions: [], setups: [], waits: 0 };
    const permission = page.locator('[data-slot="ask-card"][data-interaction-kind="permission"]');
    const setup = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"]');
    const question = page.locator('[data-slot="ask-card"]:not([data-interaction-kind="permission"]):not([data-interaction-kind="connector_setup"])');
    let busy = false;
    const deadline = Date.now() + minutes * 60000;
    while (Date.now() < deadline) {
      await page.waitForTimeout(2000);
      if (await permission.count()) {
        const card = permission.first();
        await card.scrollIntoViewIfNeeded().catch(() => {});
        await shot(`t${index + 1}-permission-${turn.permissions.length + 1}`);
        turn.permissions.push((await card.innerText()).replace(/\s+/g, " ").slice(0, 600));
        await card.getByRole("button", { name: scope }).first().click();
        await page.waitForTimeout(1500);
        continue;
      }
      if (await setup.count()) {
        const card = setup.first();
        await card.scrollIntoViewIfNeeded().catch(() => {});
        await shot(`t${index + 1}-setup-${turn.setups.length + 1}`);
        turn.setups.push((await card.innerText()).replace(/\s+/g, " ").slice(0, 600));
        const name = setupAction === "decline" ? "Decline" : "Continue";
        await card.getByRole("button", { name }).first().click();
        await page.waitForTimeout(1500);
        continue;
      }
      if (await question.count()) {
        // A question from the agent is a dead end for a script: record it and stop the wait.
        await shot(`t${index + 1}-question`);
        turn.question = (await question.first().innerText()).replace(/\s+/g, " ").slice(0, 600);
        break;
      }
      const state = await api("GET", `/v1/sessions/${sid}`).catch(() => ({}));
      if (state.status && state.status !== "idle") busy = true;
      if (busy && state.status === "idle") break;
      turn.waits += 1;
    }
    turn.seconds = Math.round((Date.now() - began) / 1000);
    Object.assign(turn, await turnRecord(sid, seen));
    await page.waitForTimeout(4000);
    if (process.env.JOURNEY_TAB === "1") {
      const open = page.getByRole("button", { name: "Open in tab" }).last();
      if (await open.count()) {
        await open.click().catch(() => undefined);
        turn.openedTab = true;
        await page.waitForTimeout(12000);
      }
    }
    const frames = await appFrames(page);
    turn.frames = frames.map(({ element, ...row }) => row);
    const last = frames.at(-1);
    if (last?.element) await last.element.scrollIntoViewIfNeeded().catch(() => {});
    await page.waitForTimeout(3000);
    await shot(`t${index + 1}-answer`);
    record.turns.push(turn);
    console.error(`[journey] turn ${index + 1}: ${turn.seconds}s, ${turn.permissions.length} permission(s), ${turn.setups.length} setup card(s), tools ${turn.tools.join(", ")}`);
  }
  record.url = page.url();
  if (process.env.JOURNEY_HERO === "1") await heroShot(page, shot, "hero");
  const out = path.join(ROOT, ".cloud-run", "journey");
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(out, `${label}.json`), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record));
}
