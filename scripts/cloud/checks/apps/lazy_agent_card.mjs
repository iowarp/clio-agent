// LZ: ask on agent load (clio-agent ledger "Lazy install, ask on agent load (LZ)").
//
// Opens a new conversation in the workspace LZ_WORKSPACE (default "lazy-a") with the agent
// LZ_AGENT (default clio/earthscope, set as the agent picker does: PATCH .../agent), waits
// for the agent-load card and reads it, then:
//   LZ_ACTION=enable  clicks Enable all and follows each item's progress until done
//                     (shots while it works), then Close;
//   LZ_ACTION=notnow  clicks Not now;
//   LZ_ACTION=none    expects no card (a second conversation after Not now).
// LZ_SEND, when set, is a plain request sent afterwards; the turn is waited for and its
// tool calls reported. Prints one JSON line.
import { api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

async function interactions(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/interactions?include_recent_resolved=true`);
  return (listed.interactions || []).filter((row) => row.payload?.intent === "agent_needs");
}

export default async function (page, shot) {
  const agent = process.env.LZ_AGENT || "clio/earthscope";
  const action = process.env.LZ_ACTION || "enable";
  const report = { agent, action };
  const log = [];
  const { session, workspace } = await openConversation(page, process.env.LZ_WORKSPACE || "lazy-a");
  report.session = session.id;
  report.workspace = workspace.id;
  await api("PATCH", `/v1/sessions/${session.id}/agent`, { ref: agent });
  await page.reload({ waitUntil: "domcontentloaded" });
  const card = page.locator('[data-intent="agent_needs"]').first();
  const shown = await card.waitFor({ timeout: 45000 }).then(
    () => true,
    () => false,
  );
  report.cardShown = shown;
  if (!shown) {
    await shot("no-card");
    report.interactions = await interactions(session.id);
  } else {
    await settle(page, 2500);
    report.title = words(await card.locator("h2, h3, [data-slot=ask-title]").first().innerText().catch(() => ""));
    report.items = await card
      .locator('[data-slot="agent-needs-item"]')
      .evaluateAll((rows) => rows.map((row) => row.innerText.replace(/\s+/gu, " ").trim()));
    report.cardText = words(await card.innerText()).slice(0, 600);
    await shot("card");
    if (process.env.LZ_SETTINGS) {
      await card.getByRole("button", { name: /^Settings/u }).click();
      await settle(page);
      await shot("card-settings");
      await card.getByRole("button", { name: /^Settings/u }).click();
    }
  }
  if (shown && action === "enable") {
    const started = Date.now();
    await card.getByRole("button", { name: /Enable all/u }).click();
    await settle(page, 4000);
    await shot("working");
    report.progress = [];
    let done = false;
    let shots = 0;
    while (Date.now() - started < 20 * 60_000) {
      const [row] = await interactions(session.id);
      const phase = row?.payload?.phase;
      const items = (row?.payload?.items || []).map((item) => `${item.id}:${item.progress}`);
      const line = `${Math.round((Date.now() - started) / 1000)}s ${phase} ${items.join(",")}`;
      if (report.progress.at(-1)?.split(" ").slice(1).join(" ") !== line.split(" ").slice(1).join(" ")) {
        report.progress.push(line);
        if (shots < 3 && phase === "working") {
          shots += 1;
          await shot(`working-${shots}`);
        }
      }
      if (phase === "done") {
        done = true;
        report.items_done = (row.payload.items || []).map((item) => ({
          id: item.id,
          progress: item.progress,
          tool_count: item.tool_count,
          reason: item.reason,
          detail: item.detail,
        }));
        break;
      }
      await page.waitForTimeout(5000);
    }
    report.seconds = Math.round((Date.now() - started) / 1000);
    report.done = done;
    await settle(page, 2500);
    report.doneText = words(await card.innerText().catch(() => "")).slice(0, 600);
    await shot("done");
    const close = card.getByRole("button", { name: /^Close/u });
    if (await close.count()) {
      await close.click();
      await settle(page, 2500);
    }
    await shot("closed");
  }
  if (shown && action === "notnow") {
    await card.getByRole("button", { name: /Not now/u }).click();
    await settle(page, 2500);
    await shot("not-now");
    const ws = await api("GET", `/v1/workspaces/${workspace.id}`).catch(() => ({}));
    report.remembered = ws.metadata?.agent_needs_not_now ?? ws.workspace?.metadata?.agent_needs_not_now;
  }
  if (process.env.LZ_SEND) {
    await send(page, process.env.LZ_SEND);
    await settle(page, 5000);
    report.idle = await waitIdle(page, session.id, log, Number(process.env.LZ_WAIT_MIN || 10));
    await settle(page, 2500);
    report.tools = await toolCalls(session.id);
    await shot("answer");
    const messages = await api("GET", `/v1/sessions/${session.id}/messages`);
    const rows = Array.isArray(messages) ? messages : messages.messages || [];
    const last = rows.filter((m) => m.role === "assistant").at(-1);
    report.answer = words((last?.parts || []).filter((p) => p.type === "text").map((p) => p.text).join(" ")).slice(0, 900);
  }
  report.log = log;
  console.log(JSON.stringify(report));
}
