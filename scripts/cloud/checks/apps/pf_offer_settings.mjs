// DOCS-F7: an offer card whose required rows are already saved keeps its four rows; the
// saved rows wait in the quiet "Settings (n)" disclosure, with Save as default inside.
// A new conversation in a workspace, a plain request, then the card as a person sees it:
// compact, Settings opened, then Add (or Not now with PF_NOT_NOW=1).
//
//   PF_WORKSPACE  workspace name (its folder is .cloud-run/apps-work/<name>)
//   PF_REQUEST    the plain request
//   PF_NOT_NOW    "1": answer Not now after the shots
// Prints one JSON line: the session, the card's rows before and after Settings, the calls.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { allowPending, api, ROOT, send, toolCalls } from "./_lib.mjs";

const env = process.env;
const words = (text) => text.replace(/\s+/gu, " ").trim();

export default async function (page, rawShot) {
  const shot = async (name) => {
    const { width, height } = page.viewportSize();
    await page.mouse.move(width - 6, Math.round(height * 0.3));
    await page.waitForTimeout(800);
    return rawShot(name);
  };
  const root = path.join(ROOT, ".cloud-run", "apps-work", env.PF_WORKSPACE);
  mkdirSync(root, { recursive: true });
  const listed = await api("GET", "/v1/workspaces");
  const rows = Array.isArray(listed) ? listed : listed.workspaces || listed.items || [];
  const workspace =
    rows.find((w) => w.root_path === root) ||
    (await api("POST", "/v1/workspaces", { name: env.PF_WORKSPACE, root_path: root }));
  const session = (await api("POST", "/v1/sessions", { workspace_id: workspace.id })).id;
  await page.goto(
    new URL(`/workspaces/${workspace.id}/sessions/${session}`, page.url()).toString(),
    { waitUntil: "domcontentloaded" },
  );
  await page.waitForTimeout(8000);
  await send(page, env.PF_REQUEST);
  const log = [];
  const card = page
    .locator('section[data-interaction-kind="connector_setup"]')
    .filter({ visible: true })
    .first();
  const until = Date.now() + 10 * 60_000;
  while (Date.now() < until && !(await card.isVisible().catch(() => false))) {
    await allowPending(session, log);
    await page.waitForTimeout(3000);
  }
  const report = { session: `${workspace.id}/${session}`, log };
  if (!(await card.isVisible().catch(() => false))) {
    report.error = "no offer card";
    console.log(JSON.stringify(report));
    return;
  }
  await card.scrollIntoViewIfNeeded();
  await page.waitForTimeout(2500);
  const box = await card.boundingBox();
  report.compact = {
    height: Math.round(box?.height ?? 0),
    inputs: await card.locator('[data-slot="connector-need-rows"]').count(),
    settings: words(
      (await card.locator('[data-slot="connector-offer-settings"]').textContent().catch(() => "")) ||
        "",
    ),
    text: words(await card.innerText()),
  };
  await shot("offer-compact");
  const settings = card.locator('[data-slot="connector-offer-settings"]');
  if (await settings.count()) {
    await settings.click();
    await page.waitForTimeout(1200);
    report.open = {
      height: Math.round((await card.boundingBox())?.height ?? 0),
      saveAsDefault: await card.getByText("Save as default").count(),
      text: words(await card.innerText()),
    };
    await shot("offer-settings-open");
    await settings.click();
    await page.waitForTimeout(800);
  }
  if (env.PF_NOT_NOW === "1") {
    await card.getByRole("button", { name: /Not now/u }).click();
  } else {
    await card.getByRole("button", { name: /^1?\s*Add$/u }).click();
    log.push("Add");
  }
  const done = Date.now() + 10 * 60_000;
  while (Date.now() < done) {
    await allowPending(session, log);
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    if (["idle", "completed", "error"].includes(status)) break;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(4000);
  await page.mouse.move(360, 500);
  await page.mouse.wheel(0, 20000);
  await page.waitForTimeout(1500);
  await shot("answered");
  report.calls = await toolCalls(session);
  console.log(JSON.stringify(report));
}
