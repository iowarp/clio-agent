// The Science log keeps its Views mode when the canvas is maximized and restored (#85).
//
// VIEWS_WORKSPACE and VIEWS_SESSION name a conversation whose study has a view. The step
// opens the Science log from the canvas menu, chooses Views, maximizes the canvas, then
// restores it, capturing each, and prints which mode is checked after each change.
export default async function (page, shot) {
  const ws = process.env.VIEWS_WORKSPACE;
  const sid = process.env.VIEWS_SESSION;
  const log = [];
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const openCanvas = page.getByRole("button", { name: "Open workspace canvas" });
  if (await openCanvas.count()) await openCanvas.first().click();
  await page.waitForTimeout(1500);
  await page.getByTitle("Open a canvas tab").click();
  await page.getByRole("menuitem", { name: "Science log" }).click();
  await page.waitForTimeout(3000);
  const views = page.getByRole("radio", { name: "Views" });
  await views.first().click();
  await page.waitForTimeout(4000);
  await shot("views");
  const mode = async () =>
    (await views.first().getAttribute("aria-checked")) ?? (await views.first().getAttribute("data-state"));
  log.push(`views before: ${await mode()}`);
  await page.getByRole("button", { name: "Maximize canvas" }).click();
  await page.waitForTimeout(4000);
  await shot("maximized");
  log.push(`views maximized: ${await mode()}`);
  await page.getByRole("button", { name: "Restore canvas beside conversation" }).click();
  await page.waitForTimeout(4000);
  await shot("restored");
  log.push(`views restored: ${await mode()}`);
  console.log(JSON.stringify({ session: sid, log }, null, 2));
}
