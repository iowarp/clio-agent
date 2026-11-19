// Measure a conversation's memory in the UI while it runs. Observe only: it answers no
// prompt and sends nothing, so run it beside the harness that drives the conversation.
//
//   OPEN_WORKSPACE=ws_… OPEN_SESSION=sess_… node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/memory_observe.mjs [--ui URL --agent URL]
//
// Every minute it prints the renderer's resident memory (from the browser's process
// list), the JS heap, DOM nodes, event listeners, images and canvases, and the
// conversation's status. MEMORY_OUT names a folder for two heap snapshots (minute 5
// and the end; MEMORY_TAG prefixes their names). LEAK_MINUTES bounds the run;
// STOP_ON_IDLE=1 stops at the first idle sample after minute 5. Measure the production
// build (scripts/cloud/serve_web_build.mjs) when judging a release: the development
// server's memory grows on its own during a live run.
import fs from "node:fs";
const OUT = process.env.MEMORY_OUT || "";
const TAG = process.env.MEMORY_TAG || "memory";
async function snapshot(client, file) {
  const chunks = [];
  const on = (e) => chunks.push(e.chunk);
  client.on("HeapProfiler.addHeapSnapshotChunk", on);
  await client.send("HeapProfiler.takeHeapSnapshot", { reportProgress: false });
  client.off("HeapProfiler.addHeapSnapshotChunk", on);
  fs.writeFileSync(file, chunks.join(""));
}
export default async function (page) {
  const ws = process.env.OPEN_WORKSPACE, sid = process.env.OPEN_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  const client = await page.context().newCDPSession(page);
  const browserCdp = await page.context().browser().newBrowserCDPSession();
  const rendererRss = async () => {
    const info = await browserCdp.send("SystemInfo.getProcessInfo");
    let max = 0;
    for (const p of info.processInfo || []) {
      if (p.type !== "renderer") continue;
      try { const st = fs.readFileSync(`/proc/${p.id}/status`, "utf8"); const kb = Number((st.match(/VmRSS:\s+(\d+)/) || [])[1] || 0); if (kb > max) max = kb; } catch {}
    }
    return Math.round(max / 1024);
  };
  await client.send("Performance.enable"); await client.send("HeapProfiler.enable");
  const minutes = Number(process.env.LEAK_MINUTES || 25);
  for (let m = 1; m <= minutes; m++) {
    await page.waitForTimeout(60000);
    const met = await client.send("Performance.getMetrics"); const g = (n) => (met.metrics.find((x) => x.name === n) || {}).value || 0;
    const st = await (await fetch(`${process.env.AGENT_URL || "http://127.0.0.1:8787"}/v1/sessions/${sid}`)).json();
    const dom = await page.evaluate(() => ({ imgs: document.images.length, canvases: document.querySelectorAll("canvas").length, iframes: document.querySelectorAll("iframe").length, imgMpx: Math.round([...document.images].reduce((a, i) => a + i.naturalWidth * i.naturalHeight, 0) / 1e6) }));
    console.log(JSON.stringify({ m, rssMB: await rendererRss(), heapMB: Math.round(g("JSHeapUsedSize") / 1e6), totalMB: Math.round(g("JSHeapTotalSize") / 1e6), nodes: g("Nodes"), listeners: g("JSEventListeners"), ...dom, status: st.status }));
    if (m === 5 && OUT) await snapshot(client, `${OUT}/${TAG}-early.heapsnapshot`);
    if (st.status === "idle" && m > 5 && process.env.STOP_ON_IDLE === "1") break;
  }
  if (OUT) await snapshot(client, `${OUT}/${TAG}-late.heapsnapshot`);
}
