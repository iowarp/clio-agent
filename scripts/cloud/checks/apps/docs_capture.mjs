// Site captures for the connectors and apps docs (site/src/content/docs/docs/connectors.mdx,
// apps.mdx): a person's plain requests in a real conversation, answered as a person would.
// Run at 1440x900 with --theme dark (the convention of site/src/assets/captures, "Connectors
// and apps"). Requests are plain and name no tool.
//
//   DOCS_MODE       new | send | watch | card | tab | picker | reopen | addurl | extensions | marketplaces
//   DOCS_WORKSPACE  workspace name for `new`; its folder is .cloud-run/apps-work/<name>
//   DOCS_COPY       files copied into that folder first, separated by ":" (`new`)
//   DOCS_SESSION    "<workspace id>/<session id>" to continue (send, tab, picker, reopen)
//   DOCS_REQUEST    the plain request (new, send)
//   DOCS_VIEW_PLUGIN "1": on an offer card, open View plugin and shoot it before Add
//   DOCS_NOT_NOW    "1": leave the first offer card waiting (shoot only)
//   DOCS_SHOW_VIEW  "1": keep the newest view card on screen while the turn runs
//   DOCS_TAB        "1": show the newest view in its tab before sending (send)
//   DOCS_HERO       "1": a hero shot of the view in its tab (tab), sidebar collapsed
// Prints one JSON line: the session, the tool calls and what the harness answered.
import { cpSync, mkdirSync } from "node:fs";
import path from "node:path";
import { allowPending, api, appFrames, heroShot, ROOT, send, showLastApp, toolCalls } from "./_lib.mjs";

const env = process.env;
const log = [];

async function goSession(page, ref) {
  const [workspace, session] = ref.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(8000);
  return session;
}

/** Answer the turn as a person: allow permissions for the session, shoot and Add each offer. */
async function answer(page, shot, session) {
  const offer = page
    .locator('[data-interaction-kind="connector_setup"]:is([data-intent="enable"], [data-intent="add_plugin"])')
    .filter({ visible: true })
    .first();
  const until = Date.now() + 15 * 60_000;
  let quiet = 0;
  let offers = 0;
  const card = page.locator('[data-mcp-app-placement="inline"]').filter({ visible: true }).last();
  while (Date.now() < until && quiet < 3) {
    await allowPending(session, log);
    // DOCS_SHOW_VIEW=1: keep the newest view card on screen, as a person watching it would,
    // so the agent can look at it (a look waits until the view is on screen).
    if (env.DOCS_SHOW_VIEW === "1" && (await card.count()))
      await card.evaluate((el) => el.scrollIntoView({ block: "center" })).catch(() => undefined);
    if (await offer.isVisible().catch(() => false)) {
      offers += 1;
      await offer.scrollIntoViewIfNeeded().catch(() => undefined);
      await page.waitForTimeout(2500);
      await shot(`offer-card-${offers}`);
      if (env.DOCS_VIEW_PLUGIN === "1" && offers === 1) {
        await offer.getByRole("button", { name: "View plugin" }).click();
        const dialog = page.getByRole("dialog").last();
        await dialog.waitFor({ state: "visible", timeout: 30000 });
        await page.waitForTimeout(3000);
        await shot("view-plugin");
        await page.keyboard.press("Escape");
        await dialog.waitFor({ state: "hidden", timeout: 10000 });
        await page.waitForTimeout(1000);
      }
      if (env.DOCS_NOT_NOW === "1") return false;
      await offer.getByRole("button", { name: /^1?\s*Add$/ }).click({ timeout: 5000 });
      log.push(`offer ${offers}: Add`);
      await page.waitForTimeout(4000);
      continue;
    }
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    quiet = ["idle", "completed", "error"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  return quiet >= 3;
}

/** The newest app card in the conversation, scrolled so its header shows at the top. */
async function showCard(page, shot) {
  const card = page.locator('[data-mcp-app-placement="inline"]').filter({ visible: true }).last();
  if (!(await card.count())) return;
  await card.evaluate((el) => el.scrollIntoView({ block: "start" }));
  await page.mouse.wheel(0, -60);
  // The globe's map tiles arrive after the frame is ready.
  await page.waitForTimeout(15000);
  await shot("view-card");
}

async function toTab(page) {
  const placed = () => page.locator('[data-mcp-app-placement="tab"]').filter({ visible: true }).count();
  if (await placed()) return;
  const open = page.getByRole("button", { name: /Open in tab|Show tab|Reopen/ }).filter({ visible: true });
  if (await open.count()) await open.last().click({ timeout: 10000 }).catch(() => undefined);
  for (let i = 0; i < 30 && !(await placed()); i += 1) await page.waitForTimeout(2000);
  await page.waitForTimeout(12000);
}

export default async function (page, rawShot) {
  // Park the pointer on the empty right edge first, so no hover card covers the shot.
  const shot = async (name) => {
    const { width, height } = page.viewportSize();
    await page.mouse.move(width - 6, Math.round(height * 0.55));
    await page.waitForTimeout(800);
    return rawShot(name);
  };
  const mode = env.DOCS_MODE;
  const report = { mode };
  let session;
  if (mode === "extensions" || mode === "marketplaces") {
    // Settings, Extensions and connectors, Discover: the catalog's plugins and connectors
    // that are off; then one of them (DOCS_CONNECTOR, default Geo) enabled from its card.
    // DOCS_MODE=marketplaces shoots the installed marketplaces instead.
    const filter = (name) =>
      page.getByRole("radio", { name }).or(page.getByRole("button", { name })).first();
    const settings = page.getByRole("link", { name: "Settings" }).first();
    if (await settings.isVisible().catch(() => false)) {
      await settings.click();
      await page.waitForTimeout(1500);
      if (!(await page.getByRole("heading", { name: "Extensions" }).count()))
        await page.getByRole("link", { name: /^Extensions/ }).first().click();
    } else {
      // A phone keeps the navigation folded: open the page by its address.
      await page.goto(new URL("/settings/plugins", page.url()).toString());
    }
    await page.locator('[role="group"][aria-label="Extensions"]').waitFor({ timeout: 30000 });
    await page.waitForTimeout(2500);
    if (mode === "marketplaces") {
      await filter(/^Marketplaces/).click();
      await page.waitForTimeout(3000);
      await shot("marketplaces");
      console.log(JSON.stringify(report));
      return;
    }
    // DOCS_FILTER: the type filter above the list (default All, so plugins and connectors).
    await filter(new RegExp(`^${env.DOCS_FILTER || "All"}`)).click();
    await page.waitForTimeout(1000);
    await filter(/^Discover/).click();
    await page.waitForTimeout(4000);
    await shot("discover");
    if (env.DOCS_ENABLE === "0") {
      console.log(JSON.stringify(report));
      return;
    }
    const name = env.DOCS_CONNECTOR || "Geo";
    await page
      .locator('[data-slot="discover-card-open"]', { hasText: new RegExp(`^${name}$`, "u") })
      .first()
      .click();
    const card = page.locator('[data-slot="connector-setup-card"]');
    await card.waitFor({ timeout: 20000 });
    await page.waitForTimeout(1500);
    await shot("card");
    await card.getByRole("button", { name: "Enable" }).click();
    const started = Date.now();
    while (Date.now() - started < 240000) {
      if (/Ready, \d+ tools|Can.t be reached|Needs /u.test(await card.innerText())) break;
      await page.waitForTimeout(2000);
    }
    await page.waitForTimeout(1500);
    await shot("card-enabled");
    console.log(JSON.stringify(report));
    return;
  }
  if (mode === "addurl") {
    // Extensions, Add, "Add by URL or command", filled for a local command (not added).
    await page.goto(new URL("/settings/plugins", page.url()).toString());
    await page.locator('[role="group"][aria-label="Extensions"]').waitFor({ timeout: 30000 });
    await page.waitForTimeout(2500);
    await page.getByRole("button", { name: "Add options" }).click();
    await page.getByRole("menuitem", { name: /Add by URL or command/u }).click();
    const dialog = page.getByRole("dialog").last();
    await dialog.waitFor({ timeout: 15000 });
    await page.waitForTimeout(1000);
    await shot("add-by-url");
    await dialog.getByRole("radio", { name: /Command/u }).click();
    await dialog.getByLabel("Name", { exact: true }).fill(env.DOCS_NAME || "hdf5");
    await dialog.getByLabel("Command", { exact: true }).fill(env.DOCS_COMMAND || "clio-kit");
    await dialog.getByLabel("Arguments", { exact: true }).fill(env.DOCS_ARGS || '["mcp-server", "hdf5"]');
    await page.waitForTimeout(1000);
    await shot("add-by-command");
    console.log(JSON.stringify(report));
    return;
  }
  if (mode === "new") {
    const root = path.join(ROOT, ".cloud-run", "apps-work", env.DOCS_WORKSPACE);
    mkdirSync(root, { recursive: true });
    for (const file of (env.DOCS_COPY || "").split(":").filter(Boolean))
      cpSync(file, path.join(root, path.basename(file)), { recursive: true });
    const listed = await api("GET", "/v1/workspaces");
    const rows = Array.isArray(listed) ? listed : listed.workspaces || listed.items || [];
    const workspace =
      rows.find((w) => w.root_path === root) ||
      (await api("POST", "/v1/workspaces", { name: env.DOCS_WORKSPACE, root_path: root }));
    const created = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
    report.session = `${workspace.id}/${created.id}`;
    session = await goSession(page, report.session);
  } else {
    report.session = env.DOCS_SESSION;
    session = await goSession(page, env.DOCS_SESSION);
  }

  if (mode === "new" || mode === "send") {
    // A view the agent should see must be on screen: show it in its tab first.
    if (env.DOCS_TAB === "1") await toTab(page);
    await send(page, env.DOCS_REQUEST);
    await page.waitForTimeout(5000);
    report.settled = await answer(page, shot, session);
    await page.waitForTimeout(5000);
    await shot("answered");
    const frames = await showLastApp(page, await appFrames(page));
    if (frames.length) await showCard(page, shot);
    report.frames = frames;
  } else if (mode === "tab") {
    await toTab(page);
    await shot("tab");
    if (env.DOCS_HERO === "1") await heroShot(page, shot, "hero");
  } else if (mode === "watch") {
    // A turn already waiting to look at a view: bring the newest card on screen, as a
    // person reading the conversation would, then let the turn finish.
    if (env.DOCS_TAB === "1") await toTab(page);
    const card = page.locator('[data-mcp-app-placement="inline"]').filter({ visible: true }).last();
    if (await card.count()) await card.evaluate((el) => el.scrollIntoView({ block: "center" }));
    await page.waitForTimeout(15000);
    report.settled = await answer(page, shot, session);
    await page.waitForTimeout(5000);
    // Scroll the conversation from its left margin: a wheel over a view would zoom it.
    await page.mouse.move(360, 500);
    await page.mouse.wheel(0, 20000);
    await page.waitForTimeout(3000);
    await shot("answered");
  } else if (mode === "card") {
    await showCard(page, shot);
  } else if (mode === "reopen") {
    await shot("before");
    await toTab(page);
    // The map tiles of a reopened globe arrive after its layers.
    await page.waitForTimeout(25000);
    await shot("reopened");
  } else if (mode === "picker") {
    const lists = page.getByRole("toolbar", { name: "Workbench lists" });
    // A closed workbench keeps its lists in the page, off to the right: open it then.
    const box = await lists.boundingBox().catch(() => null);
    if (!box || box.x + box.width > page.viewportSize().width) {
      await page.getByRole("button", { name: "Open workspace canvas" }).first().click();
      await page.waitForTimeout(1500);
    }
    await lists.getByRole("button", { name: /^Agent/u }).first().click();
    await page.waitForTimeout(2000);
    await page.locator('[data-agent-use="tools"]').first().click();
    await page.waitForTimeout(2500);
    await page.getByRole("button", { name: /Add (package|plugin)/u }).first().click();
    await page.waitForTimeout(2000);
    await shot("add-plugin");
    // The menu scrolls: its later groups (Plugins) sit below Packs.
    await page.mouse.move(1230, 400);
    await page.mouse.wheel(0, 600);
    await page.waitForTimeout(1500);
    await rawShot("add-plugin-scrolled");
  }
  report.calls = await toolCalls(session);
  report.log = log;
  console.log(JSON.stringify(report));
}
