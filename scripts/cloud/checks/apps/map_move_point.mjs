// Move one point of an editable layered map as a person does (press on it, drag, release),
// then press the map's action button. QUAL_WS, QUAL_SID, QUAL_LON/QUAL_LAT (the point),
// QUAL_DX/QUAL_DY (pixels to move it), QUAL_ACTION (the button's label).
export default async function (page, shot) {
  await page.goto(new URL(`/workspaces/${process.env.QUAL_WS}/sessions/${process.env.QUAL_SID}`, page.url()).toString());
  await page.waitForTimeout(15000);
  const surface = page.locator('[data-slot="a2ui-map-surface"]').last();
  await surface.scrollIntoViewIfNeeded();
  await page.waitForTimeout(6000);
  const at = await surface.evaluate((node, [lon, lat]) => {
    const map = node.__clioMap;
    const p = map.project([lon, lat]);
    const r = map.getCanvas().getBoundingClientRect();
    return { x: r.left + p.x, y: r.top + p.y };
  }, [Number(process.env.QUAL_LON), Number(process.env.QUAL_LAT)]);
  await shot('before');
  await page.mouse.move(at.x, at.y);
  await page.mouse.down();
  await page.mouse.move(at.x + Number(process.env.QUAL_DX || 0) / 2, at.y + Number(process.env.QUAL_DY || 0) / 2, { steps: 5 });
  await page.mouse.move(at.x + Number(process.env.QUAL_DX || 0), at.y + Number(process.env.QUAL_DY || 0), { steps: 5 });
  await page.mouse.up();
  await page.waitForTimeout(1500);
  await shot('moved');
  await page.getByRole('button', { name: process.env.QUAL_ACTION || 'Ask to replan' }).last().click();
  console.error('MOVED_AND_SENT ' + JSON.stringify(at));
  await page.waitForTimeout(5000);
  await shot('sent');
}
