// A CLIO reached by a LAN name (gact.allowed_hosts) with no gact.mcp_apps.sandbox_host:
// the card says why the view cannot open, instead of serving it with API authority.
import { check, log, openConversation, sendAndSettle } from "./_app_view.mjs";

export default async function (page, shot) {
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-remote-unconfigured");
  await sendAndSettle(page, "Open the host panel for the topic remote.");
  const alert = page.getByText("Interactive tool unavailable").last();
  await alert.waitFor({ timeout: 30000 });
  const text = await alert.locator("xpath=..").textContent();
  log("card", text);
  check(/sandbox_host/.test(text ?? ""), "the card names the setting to configure");
  check((await page.locator("[data-mcp-app-iframe]").count()) === 0, "no frame was mounted");
  await page.waitForTimeout(1000);
  await shot("unconfigured");
}
