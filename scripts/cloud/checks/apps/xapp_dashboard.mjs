// Cross-app matrix, feature 6: a plain request builds a dashboard that holds an app view
// (`clio.mcp-app.v1`) beside a chart or table; the agent's own surface capture should show
// the app's real render. Then the dashboard is opened from its card and shown as a hero
// (navigation collapsed, canvas maximized) in dark and light.
//
//   XD_APP      a short name for the app (workspace folder, labels)
//   XD_COPY     files copied into the workspace folder first (comma list, repository paths)
//   XD_REQUEST  the plain request(s); several turns are joined by " || "; "{root}" is the
//               conversation's workspace folder
//   XD_FOLLOWUP a plain request sent once the dashboard is open in the canvas
//   XD_TITLE    the dashboard's title on its card (found from the transcript when omitted)
//   XD_SESSION  "<workspace id>/<session id>": only open and shoot that conversation's dashboard
//   XD_OUT      a file that receives the JSON result (also printed as XD_RESULT)
// The captures the agent took (`<workspace>/.../a2ui-captures/*.png`) are listed so they can
// be read: they are the pixels the model received.
import { copyFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { ROOT, allowPending, api, openConversation, send } from "./_lib.mjs";

const env = process.env;
const phone = () => (globalThis.__xdPage?.viewportSize()?.width ?? 1440) < 600;
const out = { app: env.XD_APP, turns: [], log: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[xd] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

function sessionOf(page) {
  return decodeURIComponent(new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/)[1]);
}

async function settle(page, minutes = 25) {
  const sid = sessionOf(page);
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
}

async function messages(page) {
  const listed = await api("GET", `/v1/sessions/${sessionOf(page)}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  return [...rows].reverse();
}

const started = Date.now();

/** Surface captures and looks taken during this run (CLIO keeps them in its state folder). */
function captures() {
  const root = path.join(ROOT, ".cloud-run", "xdg", "state");
  const found = [];
  const walk = (dir, depth) => {
    if (depth > 8) return;
    let entries = [];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full, depth + 1);
      else if (/a2ui-captures|app-view-looks/.test(full) && entry.name.endsWith(".png")) {
        const at = statSync(full).mtimeMs;
        if (at >= started) found.push({ file: full, at });
      }
    }
  };
  walk(root, 0);
  return found.sort((a, b) => a.at - b.at).map((row) => row.file);
}

async function turn(page, shot, text, label) {
  await send(page, text);
  await page.waitForTimeout(4000);
  await settle(page);
  const rows = await messages(page);
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  const parts = rows.slice(lastUser + 1).flatMap((m) => m.parts || []);
  const result = {
    label,
    text,
    calls: parts.filter((p) => p.type === "tool_call").map((p) => p.tool_name),
    partTypes: [...new Set(parts.map((p) => p.type))],
    answer: parts.filter((p) => p.type === "text").map((p) => p.text).join("\n").trim().slice(0, 3000),
  };
  out.turns.push(result);
  if (env.XD_OUT) writeFileSync(env.XD_OUT, JSON.stringify(out, null, 2)); // kept if a later step fails
  note(`${label} calls`, result.calls);
  note(`${label} answer`, result.answer.slice(0, 1500));
  await shot(label);
}

/** The dashboard's title on its card: the last surface or document part's title. */
async function dashboardTitle(page) {
  const parts = (await messages(page)).flatMap((m) => m.parts || []);
  const surface = parts.filter((p) => /a2ui|surface|dashboard|artifact/.test(p.type) && p.title).at(-1);
  return surface?.title || "";
}

async function heroOn(page) {
  if (phone()) return;
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (navOpen) {
    const toggle = page
      .locator('[data-slot="sidebar-trigger"]')
      .or(page.getByRole("button", { name: "Toggle Sidebar" }))
      .filter({ visible: true })
      .first();
    if (await toggle.count()) await toggle.click().catch(() => undefined);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.isVisible().catch(() => false)) await maximize.click();
}

export default async function (page, shot) {
  globalThis.__xdPage = page;
  page.on("pageerror", (error) => note("pageerror", String(error?.message || error).slice(0, 300)));
  let root = "";
  if (env.XD_SESSION) {
    const [workspace, session] = env.XD_SESSION.split("/");
    await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
    await page.waitForTimeout(8000);
    root = path.join(ROOT, ".cloud-run", "apps-work", `xapp-dash-${env.XD_APP}`);
  } else {
    ({ root } = await openConversation(page, `xapp-dash-${env.XD_APP}`));
    out.session = sessionOf(page);
    for (const file of (env.XD_COPY || "").split(",").filter(Boolean)) {
      copyFileSync(path.join(ROOT, file), path.join(root, path.basename(file)));
    }
    for (const [i, text] of (env.XD_REQUEST || "").split(" || ").filter(Boolean).entries()) {
      await turn(page, shot, text.replaceAll("{root}", root), `request${i + 1}`);
    }
  }
  out.session = sessionOf(page);
  out.captures = captures();
  note("captures the agent took", out.captures);
  out.title = env.XD_TITLE || (await dashboardTitle(page));
  note("dashboard title", out.title);
  if (out.title) {
    await page.getByText(out.title, { exact: true }).last().click({ timeout: 20000 }).catch((e) => note("open card", String(e).slice(0, 200)));
  }
  const app = page.locator("[data-mcp-app-component]").filter({ visible: true }).first();
  out.appInSurface = await app.waitFor({ timeout: 120000 }).then(() => true, () => false);
  await heroOn(page);
  await page.waitForTimeout(25000); // the view starts and draws
  if (env.XD_FOLLOWUP) {
    // A plain follow-up asked while the dashboard is open beside the conversation.
    const restore = page.getByRole("button", { name: /Restore canvas|Exit maximized/ }).first();
    if (await restore.isVisible().catch(() => false)) await restore.click();
    await page.waitForTimeout(5000);
    await turn(page, shot, env.XD_FOLLOWUP, "followup");
    out.captures = captures();
    note("captures after the follow-up", out.captures);
    await heroOn(page);
    await page.waitForTimeout(10000);
  }
  for (const theme of ["dark", "light"]) {
    await page.evaluate((dark) => {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      document.documentElement.classList.toggle("dark", dark);
    }, theme === "dark");
    await page.waitForTimeout(5000);
    await shot(`dashboard-${theme}${phone() ? "" : "-hero"}`);
  }
  if (env.XD_OUT) writeFileSync(env.XD_OUT, JSON.stringify(out, null, 2));
  console.log(`XD_RESULT ${JSON.stringify(out)}`);
}
