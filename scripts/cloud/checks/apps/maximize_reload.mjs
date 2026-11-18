// Does maximizing or restoring the canvas reload an app view in a tab? Counts view-frame
// navigations around "Maximize canvas" and "Restore canvas beside conversation".
// CHECK_SESSION: a session whose latest app view can open in a tab (e.g. a spice chart).
import { api } from "./_lib.mjs";

export default async function (page, shot) {
  const sid = process.env.CHECK_SESSION;
  const session = await api("GET", `/v1/sessions/${sid}`);
  await page.goto(new URL(`/workspaces/${session.workspace_id}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(8000);
  if (!(await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').count())) {
    const open = page.getByRole("button", { name: /Open in tab|Show tab/ }).last();
    await open.click({ timeout: 30000 });
  }
  await page.locator('[data-mcp-app-placement="tab"] [data-mcp-app-iframe]').first().waitFor({ timeout: 30000 });
  await page.waitForTimeout(8000);
  const events = [];
  let phase = "settled";
  page.on("framenavigated", (f) => {
    if (f.url() === "about:srcdoc" || /\/mcp-apps\//.test(f.url())) events.push(`${phase}: ${f.url().slice(0, 50)}`);
  });
  phase = "maximize";
  await page.getByRole("button", { name: "Maximize canvas" }).first().click();
  await page.waitForTimeout(8000);
  await shot("maximized");
  phase = "restore";
  await page.getByRole("button", { name: "Restore canvas beside conversation" }).first().click();
  await page.waitForTimeout(8000);
  await shot("restored");
  console.log(JSON.stringify({ session: sid, navigations: events }, null, 2));
}
