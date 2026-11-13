// Apps check (ctx-tools stream): how a view tool's call reads in the transcript (MA13
// presentation). Opens CHECK_SESSION (a conversation where the board check ran), expands
// every tool group and every app__ row, and captures them.
export default async function (page, shot) {
  const sid = process.env.CHECK_SESSION;
  const ws = process.env.CHECK_WORKSPACE || "ws_default";
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(8000);
  for (const group of await page.getByRole("button", { name: /^Expand Called/ }).all()) {
    await group.click().catch(() => {});
  }
  await page.waitForTimeout(1000);
  for (const row of await page.getByRole("button", { name: /Set highlight|Add note|Set color/ }).all()) {
    await row.click().catch(() => {});
  }
  await page.waitForTimeout(1500);
  const rows = page.getByText(/board view/).first();
  await rows.scrollIntoViewIfNeeded().catch(() => {});
  await shot("app-tool-rows");
  const failed = page.getByText(/is not open|no longer offers/).first();
  await failed.scrollIntoViewIfNeeded().catch(() => {});
  await shot("app-tool-failed-row");
}
