// #1905 follow-up: a blocked write raises the folder-access card, and the notice's Allow
// opens a dialog that says what is granted.
//
// CARD_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS) holding notes.txt;
// CARD_OUTSIDE a folder outside the sandbox territory. The step asks for the copy with a
// tool-directed prompt (no access_paths, so the sandbox blocks it: mechanism only), answers
// nothing about folders, and captures: the conversation with the card and the notice, the
// opened terminal block, the dialog from the notice's Allow, and the state after allowing.
// It prints the pending folder cards before and after, and whether the folder was granted.
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

const folderCards = async (sid) =>
  ((await api("GET", `/v1/permissions?session_id=${sid}&status=pending`)).permissions || []).filter(
    (row) => row.kind === "folder_access",
  );

export default async function (page, shot) {
  const root = process.env.CARD_ROOT;
  const outside = process.env.CARD_OUTSIDE;
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const field = page.locator('[contenteditable="true"]').last();
  await field.click();
  await page.keyboard.type(
    `Run exactly this shell command with shell_bash and no access_paths: cp notes.txt ${outside}/notes.txt, then tell me what happened in two sentences.`,
  );
  await page.keyboard.press("Enter");
  const until = Date.now() + 6 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    for (const row of (await api("GET", `/v1/permissions?session_id=${session.id}&status=pending`))
      .permissions || []) {
      if (row.kind !== "folder_access") await api("POST", `/v1/permissions/${row.id}`, { action: "allow_session" });
    }
    const status = (await api("GET", `/v1/sessions/${session.id}`)).status;
    quiet = ["idle", "completed"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  const before = await folderCards(session.id);
  log.push(`folder cards pending after the turn: ${before.length} (${before.map((r) => r.origin).join(",")})`);
  if (process.env.CARD_ELSEWHERE) {
    // Viewing another conversation (CARD_ELSEWHERE = "<workspace>/<session>"): the card in
    // this one should raise the "Response needed" toast there.
    const [ws, sid] = process.env.CARD_ELSEWHERE.split("/");
    await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
    await page.waitForTimeout(4000);
    await shot("toast-elsewhere");
    log.push(`toast shown elsewhere: ${(await page.getByText("Response needed").count()) > 0}`);
    await page.goto(
      new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString(),
    );
    await page.waitForTimeout(4000);
  }
  await page.waitForTimeout(2000);
  await shot("card-and-notice");
  const notice = page.locator('[data-slot="turn-sandbox-notices"]').first();
  if (await notice.count()) await notice.scrollIntoViewIfNeeded();
  await shot("notice");
  // The terminal block, opened.
  const group = page.getByRole("button", { name: /ran commands/i }).first();
  if (await group.count()) await group.click();
  await page.waitForTimeout(600);
  const run = page.locator('[data-slot="tool-activity"]', { hasText: "cp notes.txt" }).first();
  if (await run.count()) {
    await run.getByText("Run", { exact: true }).first().click();
    await page.waitForTimeout(800);
    await run.scrollIntoViewIfNeeded();
    await shot("terminal");
  }
  const allow = notice.getByRole("button", { name: /^Allow / });
  if (await allow.count()) {
    await allow.first().click();
    await page.waitForTimeout(1500); // past the dialog's open animation
    await shot("dialog");
    await page.getByRole("button", { name: "Allow folder" }).click();
    await page.waitForTimeout(2500);
    await shot("allowed");
  } else log.push("no Allow on the notice");
  const after = await folderCards(session.id);
  const ws = (await api("GET", "/v1/workspaces")).find?.((w) => w.id === workspace.id) || {};
  log.push(`folder cards pending after Allow: ${after.length}`);
  log.push(`workspace folders: ${JSON.stringify((ws.source_folders || []).map((f) => f.name))}`);
  console.log(JSON.stringify({ session: session.id, workspace: workspace.id, log }, null, 2));
}
