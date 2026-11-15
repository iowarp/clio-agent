// Open each IBM Quantum view card of QUANTUM_SESSION in a tab, one after the other, and
// screenshot it; print the text each view shows.
import { api } from "./_lib.mjs";

export default async function (page, shot) {
  const sid = process.env.QUANTUM_SESSION;
  const session = await api("GET", `/v1/sessions/${sid}`);
  await page.goto(new URL(`/workspaces/${session.workspace_id}/sessions/${sid}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(10000);
  const buttons = page.getByRole("button", { name: /^(Open in tab|Show tab)$/i });
  const count = await buttons.count();
  const rows = [];
  for (let i = 0; i < count; i++) {
    await buttons.nth(i).scrollIntoViewIfNeeded().catch(() => {});
    await buttons.nth(i).click().catch((e) => rows.push({ i, click: String(e).slice(0, 120) }));
    await page.waitForTimeout(Number(process.env.QUANTUM_WAIT || 12) * 1000);
    await shot(`tab-${i + 1}`);
    const texts = [];
    for (const frame of page.frames()) {
      if (frame === page.mainFrame()) continue;
      const t = await frame.evaluate(() => document.querySelector("#app")?.innerText?.slice(0, 300) || "").catch(() => "");
      if (t) texts.push(t);
    }
    rows.push({ i, texts });
  }
  console.log(JSON.stringify({ count, rows }, null, 2));
}
