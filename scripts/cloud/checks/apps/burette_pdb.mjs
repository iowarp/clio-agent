// AQ33's first half: "show PDB 1CRN" renders the structure in Burette's Mol* view inline.
//
// Needs the burette connector (or a plain connection named burette) enabled. The request
// is plain. The step waits for the view, then reports every frame on the page and, for
// each, whether it holds a drawn WebGL canvas, so a blank view is told apart from a
// view that never loaded.
import { appFrames, openConversation, send, toolCalls, waitIdle } from "./_lib.mjs";

async function canvases(page) {
  const rows = [];
  for (const frame of page.frames()) {
    const found = await frame
      .evaluate(() =>
        Array.from(document.querySelectorAll("canvas")).map((c) => ({
          w: c.width,
          h: c.height,
          webgl: Boolean(c.getContext("webgl2") || c.getContext("webgl")),
        })),
      )
      .catch(() => []);
    if (found.length) rows.push({ frame: frame.url().slice(0, 100), canvases: found });
  }
  return rows;
}

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, "burette");
  await send(page, process.env.BURETTE_TEXT || "Show PDB 1CRN.");
  log.push(`settled: ${await waitIdle(page, session.id, log, 8)}`);
  const frames = await appFrames(page);
  if (frames.length) {
    await page.locator("iframe[data-mcp-app-iframe]").last().scrollIntoViewIfNeeded();
  }
  await page.waitForTimeout(20000); // Mol* fetches the structure and draws
  await shot("structure");
  const drawn = await canvases(page);
  console.log(JSON.stringify({ session: session.id, calls: await toolCalls(session.id), frames, drawn, log }, null, 2));
  if (!frames.length) throw new Error("no MCP App frame rendered in the conversation");
}
