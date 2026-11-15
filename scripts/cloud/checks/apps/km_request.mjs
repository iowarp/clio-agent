// Connectors moved into clio-kit (apps ledger, "Connectors moved into clio-kit (KM)"):
// one plain request in a new conversation, answered as a person would, then a hero shot
// of the view it opened (desktop only).
//
//   KM_NAME    the workspace folder name under .cloud-run/apps-work/
//   KM_SEND    the plain request; "{root}" becomes the workspace folder
//   KM_THEN    a plain follow-up sent once the first turn settles (optional)
//   KM_FILES   files (repository paths, comma-separated) copied into the folder first
//   KM_ADD     a connector's name the person adds to the conversation first, from its
//              Agent tab's Add plugin picker (connector_plugin_change.mjs), before sending
//
// Offer cards are answered Add and permission prompts "This session"
// (more_conn_answer.mjs, MC_ADD=1). Prints that step's JSON (session, calls, frames).
import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { ROOT, api, appFrames, heroShot } from "./_lib.mjs";
import change from "./connector_plugin_change.mjs";
import answer from "./more_conn_answer.mjs";

export default async function (page, shot) {
  const env = process.env;
  // The workspace and conversation are made through the API and the page is opened once,
  // by the answering step: opening it twice in a row while the dev server still serves the
  // first load's modules crashed the page on this shared machine.
  const name = env.KM_NAME || "km";
  const root = path.join(ROOT, ".cloud-run", "apps-work", name);
  mkdirSync(root, { recursive: true });
  const listed = await api("GET", "/v1/workspaces");
  const rows = Array.isArray(listed) ? listed : listed.workspaces || listed.items || [];
  const workspace =
    rows.find((w) => w.root_path === root) || (await api("POST", "/v1/workspaces", { name, root_path: root }));
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  for (const file of (env.KM_FILES || "").split(",").filter(Boolean)) {
    copyFileSync(path.join(ROOT, file), path.join(root, path.basename(file)));
  }
  if (env.KM_ADD) {
    Object.assign(env, {
      CHECK_SESSION: session.id,
      CHECK_WORKSPACE: workspace.id,
      CHECK_CONNECTOR: env.KM_ADD,
      CHECK_ACTION: "add",
    });
    await change(page, shot);
  }
  env.MC_SESSION = `${workspace.id}/${session.id}`;
  env.MC_ADD = "1";
  env.MC_SEND = env.KM_SEND.replaceAll("{root}", root);
  await answer(page, shot);
  if (env.KM_THEN) {
    env.MC_SEND = env.KM_THEN.replaceAll("{root}", root);
    await answer(page, shot);
  }
  const wide = (page.viewportSize()?.width ?? 1440) > 700;
  const frames = await appFrames(page);
  if (wide && frames.length) {
    const open = page.getByRole("button", { name: "Open in tab" }).last();
    if (await open.count()) {
      await open.click().catch(() => undefined);
      await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first()
        .waitFor({ timeout: 30000 }).catch(() => undefined);
    }
    await heroShot(page, shot, "hero");
  }
}
