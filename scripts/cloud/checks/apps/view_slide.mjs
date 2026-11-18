// A site slide: open a conversation (QUAL_WS, QUAL_SID) with the sidebar collapsed, bring
// a component (QUAL_ID) into view, let it draw (QUAL_SETTLE_MS) and capture the window.
export default async function (page, shot) {
  const agent = "http://127.0.0.1:8787";
  await page.goto(new URL(`/workspaces/${process.env.QUAL_WS}/sessions/${process.env.QUAL_SID}`, page.url()).toString());
  await page.waitForTimeout(12000);
  if (await page.getByText("What should we investigate?").count()) {
    const session = await (await fetch(`${agent}/v1/sessions/${process.env.QUAL_SID}`)).json();
    await page.getByText(session.title, { exact: true }).first().click();
    await page.waitForTimeout(10000);
  }
  const toggle = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  if (await toggle.count()) { await toggle.click(); await page.waitForTimeout(1500); }
  const node = page.locator(`[data-a2ui-component-id="${process.env.QUAL_ID}"]`).last();
  const vp = page.viewportSize();
  await page.mouse.move(vp.width / 2, vp.height / 2);
  for (let i = 0; i < 80 && !(await node.count()); i += 1) {
    await page.mouse.wheel(0, -600);
    await page.waitForTimeout(250);
  }
  if (!(await node.count())) { console.log(JSON.stringify({ missing: process.env.QUAL_ID })); return; }
  await node.scrollIntoViewIfNeeded();
  const offset = Number(process.env.QUAL_OFFSET || 0);
  if (offset) await page.mouse.wheel(0, offset);
  await page.waitForTimeout(Number(process.env.QUAL_SETTLE_MS || 6000));
  await shot("slide");
}
