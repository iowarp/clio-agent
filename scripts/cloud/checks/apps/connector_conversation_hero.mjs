// Shots of a conversation that has a connector's view (CN10 *Amended*): the conversation
// as it reads, then, at desktop width, a hero shot with the left navigation collapsed and
// the view maximized in its tab. At phone width only the conversation is shot.
//   CHECK_SESSION (sess_...), CHECK_WORKSPACE (its workspace)
import { heroShot } from "./_lib.mjs";

export default async function (page, shot) {
  const session = process.env.CHECK_SESSION;
  const workspace = process.env.CHECK_WORKSPACE || "ws_default";
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  await shot("conversation");
  const phone = (page.viewportSize()?.width ?? 1440) < 700;
  const report = { phone };
  if (!phone) {
    // A view whose window closed offers Reopen (MA19): reopen the latest first.
    const reopen = page.getByRole("button", { name: "Reopen" }).last();
    if (await reopen.count()) {
      await reopen.click().catch(() => undefined);
      await page.waitForTimeout(15000);
    }
    if (!(await page.locator("[data-mcp-app-tab]").count())) {
      const open = page.getByRole("button", { name: "Open in tab" }).last();
      if (await open.count()) await open.click().catch(() => undefined);
    }
    await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 60000 }).catch(() => undefined);
    report.tab = await page.locator("[data-mcp-app-tab]").count();
    await page.waitForTimeout(15000);
    await heroShot(page, shot, "hero");
  } else {
    await page.mouse.wheel(0, 4000);
    await page.waitForTimeout(3000);
    await shot("conversation-end");
  }
  console.error(JSON.stringify(report));
}
