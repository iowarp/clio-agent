// AQ32: the spice connector simulates a netlist with ngspice and the waveform chart
// renders in the conversation (plot_waveform's MCP App), not in an external browser.
//
// Needs the spice connector (or a plain connection named spice) enabled. The netlist
// tests/fixtures/apps/rc_lowpass.cir is copied into a fresh workspace folder; the two
// requests are plain: they name the simulator and the node, never a tool.
import { copyFileSync } from "node:fs";
import path from "node:path";
import { ROOT, appFrames, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

export default async function (page, shot) {
  const log = [];
  const { root, session } = await openConversation(page, "spice");
  copyFileSync(path.join(ROOT, "tests/fixtures/apps/rc_lowpass.cir"), path.join(root, "rc_lowpass.cir"));

  await send(page, `Simulate the circuit in ${path.join(root, "rc_lowpass.cir")} with ngspice.`);
  log.push(`simulate settled: ${await waitIdle(page, session.id, log, 8)}`);
  await shot("simulated");

  await send(page, "Show me V(out).");
  log.push(`plot settled: ${await waitIdle(page, session.id, log, 8)}`);
  await page.waitForTimeout(5000);
  const frames = await appFrames(page);
  if (frames.length) {
    await page.locator("iframe[data-mcp-app-iframe]").last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(2000);
  }
  await shot("waveform");
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), frames, log }, null, 2));
  if (!frames.length) throw new Error("no MCP App frame rendered in the conversation");
}
