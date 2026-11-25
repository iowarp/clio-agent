// Count each layered map's render events over 3 s after load: a settled map renders ~0.
export default async function (page, shot) {
  await page.goto(new URL(`/workspaces/${process.env.QUAL_WS}/sessions/${process.env.QUAL_SID}`, page.url()).toString());
  await page.waitForTimeout(15000);
  const counts = await page.evaluate(async () => {
    const maps = [...document.querySelectorAll('[data-slot="a2ui-map-surface"]')].map((n) => n.__clioMap).filter(Boolean);
    const tallies = maps.map(() => 0);
    maps.forEach((m, i) => m.on('render', () => { tallies[i] += 1; }));
    await new Promise((r) => setTimeout(r, 3000));
    return { maps: maps.length, renders: tallies };
  });
  console.error('SETTLE ' + JSON.stringify(counts));
  await shot('settle');
}
