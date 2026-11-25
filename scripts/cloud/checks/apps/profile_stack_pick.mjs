// A profile stack (QUAL_ID) in a conversation: capture it, pick a curve, exclude it, pick another.
export default async function (page, shot) {
  const agent = "http://127.0.0.1:8787";
  await page.goto(new URL(`/workspaces/${process.env.QUAL_WS}/sessions/${process.env.QUAL_SID}`, page.url()).toString());
  await page.waitForTimeout(12000);
  if (await page.getByText("What should we investigate?").count()) {
    const session = await (await fetch(`${agent}/v1/sessions/${process.env.QUAL_SID}`)).json();
    await page.getByText(session.title, { exact: true }).first().click();
    await page.waitForTimeout(10000);
  }
  const node = process.env.QUAL_TITLE
    ? page.getByRole("region", { name: process.env.QUAL_TITLE }).first()
    : page.locator(`[data-a2ui-component-id="${process.env.QUAL_ID}"]`).last();
  const vp = page.viewportSize();
  await page.mouse.move(vp.width / 2, vp.height / 2);
  for (let i = 0; i < 80 && !(await node.count()); i += 1) { await page.mouse.wheel(0, -600); await page.waitForTimeout(250); }
  await node.scrollIntoViewIfNeeded();
  await page.waitForTimeout(6000);
  await shot("stack");
  const canvas = node.locator("canvas").first();
  await canvas.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(1500);
  const box = await canvas.boundingBox();
  // Walk across the plot at mid-depth until a curve is under the pointer.
  let picked = false;
  for (let fx = 0.1; fx < 0.95 && !picked; fx += 0.02) {
    await page.mouse.move(box.x + box.width * fx, box.y + box.height * 0.55);
    await page.waitForTimeout(120);
    if (await node.getByRole("status").count()) {
      await page.mouse.click(box.x + box.width * fx, box.y + box.height * 0.55);
      picked = true;
    }
  }
  await page.waitForTimeout(800);
  await canvas.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await shot("picked");
  const exclude = node.getByRole("button", { name: "Exclude", exact: true });
  if (await exclude.count()) { await exclude.click(); await page.waitForTimeout(800); }
  // Pick a second curve so the strip shows it with the first one excluded.
  for (let fx = 0.12; fx < 0.95; fx += 0.02) {
    await page.mouse.move(box.x + box.width * fx, box.y + box.height * 0.45);
    await page.waitForTimeout(120);
    if (await node.getByRole("status").count()) { await page.mouse.click(box.x + box.width * fx, box.y + box.height * 0.45); break; }
  }
  await page.waitForTimeout(800);
  await shot("excluded");
  console.log(JSON.stringify({ picked }));
}
