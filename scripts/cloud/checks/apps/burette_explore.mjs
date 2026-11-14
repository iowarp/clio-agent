// Exploratory: open the Burette view in a tab and list its visible controls, before and
// after pressing CHECK_PRESS (an aria-label). Used to find how a person selects residues.
import { isViewDocument, openConversation, send, waitIdle } from "./_lib.mjs";

async function controls(frame) {
  return frame.evaluate(() =>
    [...document.querySelectorAll("button, [role=button], [role=menuitem], [role=switch], input")]
      .map((b) => ({ b, r: b.getBoundingClientRect() }))
      .filter(({ r }) => r.width > 0 && r.height > 0 && r.x < innerWidth && r.y < innerHeight && r.x >= 0)
      .map(({ b, r }) => `${Math.round(r.x)},${Math.round(r.y)} ${b.getAttribute("aria-label") || b.getAttribute("title") || ""} | ${(b.textContent || "").trim().slice(0, 40)}`),
  );
}

export default async function (page, shot) {
  const log = [];
  const { session } = await openConversation(page, "burette");
  await send(page, "Show PDB 1CRN.");
  log.push(`settled: ${await waitIdle(page, session.id, log, 8)}`);
  await page.waitForTimeout(8000);
  await page.getByRole("button", { name: "Open in tab" }).first().click();
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 30000 });
  await page.waitForTimeout(25000);
  const frame = page.frames().filter(isViewDocument).at(-1);
  const before = await controls(frame);
  await shot("tab");
  let after = [];
  for (const label of (process.env.CHECK_PRESS || "").split(",").filter(Boolean)) {
    await frame.getByRole("button", { name: label }).first().click();
    await page.waitForTimeout(2000);
    after.push(label, ...(await controls(frame)));
    await shot(`after-${label.replace(/\W+/gu, "-").toLowerCase()}`);
  }
  console.log(JSON.stringify({ session: session.id, before, after, log }, null, 2));
}
