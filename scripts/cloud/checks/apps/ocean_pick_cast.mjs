// Click a cast on a section (QUAL_ID) and capture the linked views before and after.
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
  const section = page.locator(`[data-a2ui-component-id="${process.env.QUAL_ID}"]`).last();
  for (let i = 0; i < 80 && !(await section.count()); i += 1) {
    await page.mouse.wheel(0, -600);
    await page.waitForTimeout(250);
  }
  await section.scrollIntoViewIfNeeded();
  const offset = Number(process.env.QUAL_OFFSET || 0);
  if (offset) {
    const vp = page.viewportSize();
    await page.mouse.move(vp.width / 2, vp.height / 2);
    await page.mouse.wheel(0, offset);
  }
  await page.waitForTimeout(8000);
  await shot("before");
  const canvas = section.locator("canvas").first();
  const box = await canvas.boundingBox();
  const fx = Number(process.env.QUAL_FX || 0.3);
  await page.mouse.click(box.x + box.width * fx, box.y + box.height * 0.35);
  await page.waitForTimeout(4000);
  await shot("picked");
  console.log(JSON.stringify({ clicked: [box.x + box.width * fx, box.y + box.height * 0.35] }));
}
