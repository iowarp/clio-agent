// Capture a conversation's surface (QUAL_WS, QUAL_SID, component QUAL_ID, default root)
// with the production capture function and compare the image with the live layout:
// the image height must equal the live height, and it is saved for inspection.
import { writeFileSync } from "node:fs";

export default async function (page, shot) {
  await page.goto(new URL(`/workspaces/${process.env.QUAL_WS}/sessions/${process.env.QUAL_SID}`, page.url()).toString());
  await page.waitForTimeout(12000);
  const id = process.env.QUAL_ID || "root";
  const node = page.locator(`[data-a2ui-component-id="${id}"]`).last();
  const vp = page.viewportSize();
  await page.mouse.move(vp.width / 2, vp.height / 2);
  for (let i = 0; i < 80 && !(await node.count()); i += 1) {
    await page.mouse.wheel(0, -600);
    await page.waitForTimeout(250);
  }
  await node.scrollIntoViewIfNeeded();
  await page.waitForTimeout(8000);
  // QUAL_HOVER: the pointer rests on the view, as in a live session (hover shows its toolbar).
  if (process.env.QUAL_HOVER) {
    const b = await node.boundingBox();
    if (b) await page.mouse.move(b.x + b.width / 2, b.y + 20);
    await page.waitForTimeout(800);
  }
  const result = await page.evaluate(async ({ id: componentId, within }) => {
    // A component wrapper may lay out as contents; its child then holds the box (as the capture does).
    const box = (n) => (n.getBoundingClientRect().height > 0 ? n : n.firstElementChild);
    const shownAll = (cid) => [...document.querySelectorAll(`[data-a2ui-component-id="${cid}"]`)].map(box).filter((n) => n && n.getBoundingClientRect().height > 0);
    const process_inner = (cid) => {
      // The surface: the smallest displayed element holding both named components.
      if (!within) return shownAll(cid).pop();
      const a = shownAll(within).pop();
      const b = shownAll(cid).find((n) => n.contains(a)) ?? shownAll(cid)[0];
      let up = a; while (up && !up.contains(b)) up = up.parentElement;
      return up;
    };
    const target = process_inner(componentId);
    const mod = await import("/src/components/clio/a2ui-region-capture.tsx");
    const rect = target.getBoundingClientRect();
    const blob = await mod.captureRenderedSurfacePng(target, 1);
    const bitmap = await createImageBitmap(blob);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return { live: [Math.round(rect.width), Math.round(rect.height)], image: [bitmap.width, bitmap.height], png: btoa(binary) };
  }, { id, within: process.env.QUAL_WITHIN || "" });
  const out = `/home/user/clio-agent/.cloud-run/shots/capture-fidelity-${process.env.QUAL_LABEL || id}.png`;
  writeFileSync(out, Buffer.from(result.png, "base64"));
  console.log(JSON.stringify({ live: result.live, image: result.image, saved: out }));
  await shot("live");
}
