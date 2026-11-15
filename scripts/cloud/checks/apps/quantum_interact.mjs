// IBM Quantum views worked by the person (ibm-quantum ledger QQ8 to QQ10).
//
// A plain request opens the layout view; then, as a person would: step to a layer with
// the slider, re-transpile at level 3 from the view, select a gate in its logical
// circuit, and ask about "the gate I selected" in a plain follow-up. Prints the view's
// header before and after, the step text, the selection line, the app context CLIO
// stored and the agent's answer.
//   QUANTUM_TEXT    the opening request (default: a 5-qubit GHZ on the simulated Heron device)
//   QUANTUM_ASK     the follow-up about the selection
import { api, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

const text = async (loc) => ((await loc.textContent({ timeout: 5000 }).catch(() => "")) ?? "").replace(/\s+/g, " ").trim();

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, process.env.QUANTUM_WORKSPACE || "quantum");
  await api("PATCH", `/v1/sessions/${session.id}/agent`, { add: ["clio/ibm-quantum"] });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(4000);
  await send(page, process.env.QUANTUM_TEXT || "Show me how a 5-qubit GHZ circuit lands on the simulated Heron device.");
  log.push(`opened: ${await waitIdle(page, session.id, log, 10)}`);
  await page.waitForTimeout(8000);
  // The live layout view: CLIO's sandbox frame, then the view's own frame.
  const host = page.locator("[data-mcp-app-iframe]").last();
  await host.scrollIntoViewIfNeeded();
  const view = page.frameLocator("[data-mcp-app-iframe] >> nth=-1").frameLocator("iframe");
  const facts = view.locator(".facts");
  const before = await text(facts);
  await shot("layout-opened");
  const slider = view.locator('input[type="range"]');
  const max = Number(await slider.getAttribute("max").catch(() => "0"));
  const mid = Math.floor(max / 2);
  await slider.fill(String(mid));
  await page.waitForTimeout(800);
  const step = await text(view.locator(".tools .selection"));
  await shot(`layer-${mid}`);
  await view.locator('select[aria-label="Optimization level"]').selectOption("3");
  await view.getByRole("button", { name: "Re-transpile" }).click();
  await page.waitForTimeout(15000);
  const after = await text(facts);
  const note = await text(view.locator(".selection").last());
  await shot("level-3");
  // Select the second gate of the logical circuit.
  const gate = view.locator('[aria-label^="Gate 1:"]').first();
  await gate.click();
  await page.waitForTimeout(2500);
  const selected = await text(view.locator(".selection").first());
  await shot("gate-selected");
  const context = await api("GET", `/v1/sessions/${session.id}`).then((s) => (s.metadata || {}).mcp_app_context || {}).catch(() => ({}));
  await send(page, process.env.QUANTUM_ASK || "Which physical qubits run the gate I selected, and why there?");
  log.push(`asked: ${await waitIdle(page, session.id, log, 10)}`);
  await page.waitForTimeout(3000);
  await shot("answer");
  const messages = await api("GET", `/v1/sessions/${session.id}/messages?limit=4`);
  const rows = messages.messages || messages.items || messages;
  const answer = (rows.at(-1)?.parts || []).filter((p) => p.type === "text").map((p) => p.text).join("\n");
  console.log(JSON.stringify({ session: session.id, before, step, after, note, selected,
    context: Object.values(context).map((c) => c.context), answer: answer.slice(0, 1500),
    calls: await toolCalls(session.id), log }, null, 2));
}
