// A sandbox block in one conversation raises "Response needed" while another is open.
//
// ELSEWHERE ("<workspace>/<session>") is the conversation the browser shows; ATT_ROOT a fresh
// workspace folder holding notes.txt, ATT_OUTSIDE a folder outside the sandbox territory.
// The step opens ELSEWHERE, starts a blocked copy in a new conversation through the API
// (tool-directed prompt, mechanism only), answers the command's own approval, and captures
// the toast when it shows (it closes after a few seconds), printing whether the toast and the
// bell show the card.
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

export default async function (page, shot) {
  const [ws, sid] = process.env.ELSEWHERE.split("/");
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const root = process.env.ATT_ROOT;
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await api("POST", `/v1/sessions/${session.id}/messages`, {
    text: `Run exactly this shell command with shell_bash and no access_paths: cp notes.txt ${process.env.ATT_OUTSIDE}/notes.txt, then say what happened in one sentence.`,
  });
  // A toast closes after a few seconds, so it is watched for while the card is awaited
  // and for a while after, and the page is captured the moment it shows.
  const toastShown = () => page.getByText("Response needed").count();
  let card = false;
  let toast = false;
  let cardAt = 0;
  for (let i = 0; i < 300 && !(card && (toast || Date.now() - cardAt > 45_000)); i += 1) {
    if (!card) {
      const rows = (await api("GET", `/v1/permissions?session_id=${session.id}&status=pending`)).permissions || [];
      for (const row of rows)
        if (row.kind === "folder_access") [card, cardAt] = [true, Date.now()];
        else await api("POST", `/v1/permissions/${row.id}`, { action: "allow_session" });
    }
    if (!toast && (await toastShown()) > 0) {
      toast = true;
      await shot("toast");
    }
    await page.waitForTimeout(1000);
  }
  await shot("elsewhere");
  console.log(
    JSON.stringify({
      session: session.id,
      card,
      toast,
      toastSecondsAfterCard: toast && cardAt ? Math.round((Date.now() - cardAt) / 1000) : null,
      bell: (await page.getByRole("button", { name: /needs your response/ }).count()) > 0,
    }),
  );
}
