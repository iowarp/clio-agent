// IBM Quantum scenarios (ibm-quantum ledger QQ*): one or more plain requests in a fresh
// conversation, then the last view brought into sight, screenshots, and every tool call.
//
// QUANTUM_TEXT holds the requests, separated by "||" (each waits for its turn to settle).
// QUANTUM_WAIT is the seconds to wait after the last turn for views to draw (default 6).
// QUANTUM_CARD=decline presses "Not now" on a connector card that appears during a turn
// (after a screenshot of it), as the person would; =submit presses its first button.
// QUANTUM_ADD adds plugins to the conversation first, as the person's "Add plugin" menu
// does (PATCH /v1/sessions/{sid}/agent with `add`), for example "clio/ibm-quantum".
import { api, appFrames, openConversation, send, showLastApp, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  const texts = (process.env.QUANTUM_TEXT || "Make a Bell state and simulate it.").split("||");
  let session;
  if (process.env.QUANTUM_SESSION) {
    session = await api("GET", `/v1/sessions/${process.env.QUANTUM_SESSION}`);
    await page.goto(new URL(`/workspaces/${session.workspace_id}/sessions/${session.id}`, page.url()).toString(), {
      waitUntil: "domcontentloaded",
    });
    await page.waitForTimeout(8000);
  } else {
    ({ session } = await openConversation(page, process.env.QUANTUM_WORKSPACE || "quantum"));
  }
  if (process.env.QUANTUM_ADD) {
    const add = process.env.QUANTUM_ADD.split(",").map((s) => s.trim()).filter(Boolean);
    await api("PATCH", `/v1/sessions/${session.id}/agent`, { add });
    log.push(`added: ${add.join(", ")}`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForTimeout(4000);
  }
  let watching = true;
  const cards = [];
  const watcher = (async () => {
    while (watching && process.env.QUANTUM_CARD) {
      const listed = await api("GET", `/v1/sessions/${session.id}/interactions`).catch(() => ({}));
      const pending = (listed.interactions || []).filter((x) => x.kind === "connector_setup" && x.status === "pending");
      for (const card of pending) {
        if (cards.includes(card.id)) continue;
        cards.push(card.id);
        await page.waitForTimeout(2500);
        await shot(`card-${cards.length}`);
        const name = process.env.QUANTUM_CARD === "decline" ? /^Not now/ : /^(Add|Continue|Save)/;
        const pressed = await page.getByRole("button", { name }).last().click({ timeout: 10000 }).then(() => true, () => false);
        log.push(`card ${card.id} (${JSON.stringify((card.payload || {}).intent)} ${JSON.stringify(Object.keys((card.payload || {})))}): pressed ${pressed}`);
        if (!pressed) await api("POST", `/v1/sessions/${session.id}/interactions/${card.id}/respond`, { action: process.env.QUANTUM_CARD === "decline" ? "decline" : "submit" });
      }
      await page.waitForTimeout(2000);
    }
  })();
  for (const [i, text] of texts.entries()) {
    await send(page, text.trim());
    log.push(`turn ${i + 1} settled: ${await waitIdle(page, session.id, log, Number(process.env.QUANTUM_MINUTES || 10))}`);
    await page.waitForTimeout(Number(process.env.QUANTUM_WAIT || 6) * 1000);
    await shot(`turn-${i + 1}`);
  }
  watching = false;
  await watcher;
  const all = await appFrames(page);
  for (const [i, frame] of all.entries()) {
    if (!frame.element) continue;
    await frame.element.scrollIntoViewIfNeeded().catch(() => {});
    await page.waitForTimeout(2500);
    await shot(`frame-${i + 1}`);
  }
  const frames = await showLastApp(page, all);
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), frames, log }, null, 2));
}
