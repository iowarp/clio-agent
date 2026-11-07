// A stream in the side panel: open a conversation, open its Session artifacts in the canvas,
// open the stream, and capture it twice to show it updating.
//
// PANEL_WORKSPACE and PANEL_SESSION name the conversation; PANEL_STREAM the stream's name;
// PANEL_WAIT_S (default 10) the seconds between the two captures. It prints the stream
// content requests the page sent while the stream was open, and the view's state line.
const AGENT = process.env.AGENT_URL || "http://127.0.0.1:8787";

export default async function (page, shot) {
  const workspace = process.env.PANEL_WORKSPACE;
  const session = process.env.PANEL_SESSION;
  const name = process.env.PANEL_STREAM || "sensor";
  const wait = Number(process.env.PANEL_WAIT_S || 10) * 1000;
  const reads = [];
  page.on("request", (request) => {
    if (/\/v1\/streams\/[^/]+\/content/u.test(request.url())) reads.push(Date.now());
  });
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const log = [];
  // A closed canvas stays mounted off screen; open it from the conversation's toolbar.
  const toggle = page.getByRole("button", { name: "Open workspace canvas" });
  if (await toggle.count()) {
    await toggle.first().click();
    await page.waitForTimeout(1500);
  }
  await page.getByRole("button", { name: "Open a canvas tab" }).first().click();
  await page.getByRole("menuitem", { name: /Session artifacts/u }).click();
  await page.waitForTimeout(2000);
  await shot("artifacts");
  const list = page.getByRole("region", { name: "Session artifact list" });
  const row = list.getByText(name, { exact: true }).first();
  if (await row.count()) await row.click();
  else log.push(`no row named ${name}`);
  await page.waitForTimeout(3000);
  await shot("stream");
  const before = reads.length;
  await page.waitForTimeout(wait);
  await shot("stream-later");
  const state = page.locator('[data-slot="stream-state"]').first();
  console.log(
    JSON.stringify(
      {
        agent: AGENT,
        stateLine: (await state.count()) ? await state.innerText() : null,
        contentReads: reads.length,
        contentReadsDuringWait: reads.length - before,
        log,
      },
      null,
      2,
    ),
  );
}
