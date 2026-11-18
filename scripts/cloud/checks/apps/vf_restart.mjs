// View call fixes (VF, MC-F2): CLIO restarts while a view's long-poll is in flight. The
// view's call is the view's, not the agent's: it must end without waking the agent, so no
// unprompted turn appears afterwards.
//
//   VF_SESSION  "<workspace id>/<session id>" whose latest view keeps a call in flight
//               (the PDF viewer polls for commands every 30 s)
//   VF_WAIT_S   seconds to watch the conversation after the restart (default 150)
// Runs `stack.sh restart` with the view open, opens the conversation again, waits, and
// prints the message count and turn states before and after, the view's cut-off calls in
// the task ledger, and the conversation's last message.
import { execSync } from "node:child_process";
import { AGENT, ROOT, api, appFrames, showLastApp } from "./_lib.mjs";

const sleep = (page, ms) => page.waitForTimeout(ms);

async function waitHttp(url) {
  for (let i = 0; i < 120; i += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error(`${url} did not come up`);
}

async function messages(session) {
  const body = await api("GET", `/v1/sessions/${session}/messages`);
  const rows = Array.isArray(body) ? body : body.messages || body.items || [];
  return rows.map((m) => ({ id: m.id, role: m.role, origin: m.metadata?.origin || "" }));
}

async function cutOff(session) {
  const body = await api("GET", `/v1/sessions/${session}/tasks/query?limit=500`);
  return (body.tasks || [])
    .filter((t) => t.status === "cancelled" || t.status === "task_state_uncertain" || t.attention_episode)
    .map((t) => `${t.tool} ${t.status} ${t.settlement} ${t.raw_status}${t.attention_episode ? " episode" : ""}`);
}

export default async function (page, shot) {
  const [workspace, session] = process.env.VF_SESSION.split("/");
  const waitS = Number(process.env.VF_WAIT_S || 150);
  const url = new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString();
  await page.goto(url);
  await sleep(page, 8000);
  const show = page.getByRole("button", { name: /^(Open in tab|Show tab)$/ }).filter({ visible: true });
  if (await show.count()) await show.last().click().catch(() => undefined);
  await sleep(page, 20000); // the view mounts, reads its file and starts polling
  await showLastApp(page, await appFrames(page));
  await shot("view-polling-before-restart");
  const before = await messages(session);
  const ledgerBefore = await cutOff(session);
  execSync("bash scripts/cloud/stack.sh restart", { stdio: "ignore", cwd: ROOT });
  await waitHttp(`${AGENT}/v1/health`);
  await waitHttp(`${AGENT}/v1/workspaces`);
  await page.goto(url);
  await sleep(page, waitS * 1000);
  await shot("after-restart-no-unprompted-turn");
  const after = await messages(session);
  const ledgerAfter = await cutOff(session);
  const state = (await api("GET", `/v1/sessions/${session}`)).state;
  return {
    session,
    messagesBefore: before.length,
    messagesAfter: after.length,
    added: after.slice(before.length),
    state,
    cutOffBefore: ledgerBefore,
    cutOffAfter: ledgerAfter,
  };
}
