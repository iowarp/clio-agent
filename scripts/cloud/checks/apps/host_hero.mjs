// Desktop "hero" shot of an app in its tab (1440x900): the left navigation collapsed
// and the workbench panel dragged wide, the conversation beside it.
import { openConversation, sendAndSettle } from "./_app_view.mjs";

export default async function (page, shot) {
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-panel-hero");
  await sendAndSettle(page, process.env.CHECK_TEXT || "Open the host panel for the topic hero.");
  // The card's own control, so any app's view can be shown this way.
  await page.getByRole("button", { name: "Open in tab" }).last().click();
  await page.locator("[data-mcp-app-tab] [data-mcp-app-iframe]").first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(Number(process.env.CHECK_SETTLE_MS || 4000));
  await page.locator('[data-slot="sidebar-trigger"]').first().click().catch(() => {});
  await page.waitForTimeout(800);
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" });
  const box = await handle.boundingBox().catch(() => null);
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(480, box.y + box.height / 2, { steps: 12 });
    await page.mouse.up();
  }
  await page.waitForTimeout(1500);
  await shot("hero");
}
