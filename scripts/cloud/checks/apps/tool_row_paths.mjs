// H3 and H5 (fix2): the row of a tool call in a finished conversation, expanded.
//
//   ROW_SESSION=<sid> ROW_WORKSPACE=<wid> ROW_TITLE="Open a ParaView view" \
//     node scripts/cloud/ui_check.mjs --agent … --ui … --steps scripts/cloud/checks/apps/tool_row_paths.mjs
//
// Opens the conversation, expands every collapsed group of tool calls, then the first row
// whose text matches ROW_TITLE, and shoots it. It logs the row's text so the run records
// whether a provenance warning ("Provenance incomplete", "degraded") or a sandbox note
// ("CLIO's sandbox") is shown.
export default async function (page, shot) {
  const sid = process.env.ROW_SESSION;
  const wid = process.env.ROW_WORKSPACE;
  const title = new RegExp(process.env.ROW_TITLE || "Open a ParaView view", "i");
  await page.goto(new URL(`/workspaces/${wid}/sessions/${sid}`, page.url()).toString());
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 }).catch(() => undefined);
  await page.waitForTimeout(6000);
  const groups = page.getByRole("button", { name: /Called \d+ tools?/ }).filter({ visible: true });
  for (let i = 0; i < (await groups.count()); i += 1) {
    await groups.nth(i).click().catch(() => undefined);
    await page.waitForTimeout(400);
  }
  const row = page.getByRole("button", { name: title }).filter({ visible: true }).first();
  if (await row.count()) {
    await row.scrollIntoViewIfNeeded().catch(() => undefined);
    await row.click().catch(() => undefined);
    await page.waitForTimeout(1500);
  } else {
    console.error("[row] no row matching", String(title));
  }
  const text = await page.locator("main").innerText().catch(() => "");
  console.error(
    "[row] shows",
    JSON.stringify({
      provenance_incomplete: /Provenance incomplete/i.test(text),
      degraded: /\bdegraded\b/i.test(text),
      sandbox_note: /CLIO's sandbox/i.test(text),
    }),
  );
  await shot("tool-row");
}
