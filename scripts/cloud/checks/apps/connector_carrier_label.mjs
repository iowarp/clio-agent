// The plugin a connector brings is labelled "From connector <name>" in the agent's
// plugin tree, not "Added by you". CHECK_SESSION / CHECK_WORKSPACE name a conversation
// whose agent carries an enabled connector.
export default async function (page, shot) {
  const { CHECK_SESSION: session, CHECK_WORKSPACE: workspace } = process.env;
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(4000);
  // The workbench lists sit in the right sidebar: open it.
  const lists = page.getByRole("toolbar", { name: "Workbench lists" });
  for (const name of ["Open workspace canvas", "Toggle Sidebar"]) {
    if (await lists.isVisible().catch(() => false)) break;
    const buttons = page.getByRole("button", { name });
    for (let i = (await buttons.count()) - 1; i >= 0; i--) {
      if (await lists.isVisible().catch(() => false)) break;
      await buttons.nth(i).click({ timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(1500);
    }
  }
  await shot("workbench");
  console.log(
    JSON.stringify(
      await page
        .getByRole("toolbar", { name: "Workbench lists" })
        .getByRole("button")
        .evaluateAll((els) => els.map((e) => [e.getAttribute("aria-label") || e.textContent, e.checkVisibility()])),
    ),
  );
  const agent = page
    .getByRole("toolbar", { name: "Workbench lists" })
    .getByRole("button", { name: /^Agent/u });
  await agent.first().dispatchEvent("click"); // the open sidebar overlays its own toolbar edge
  await page.waitForTimeout(2500);
  const tools = page.locator('[data-agent-use="tools"]');
  if (await tools.count()) await tools.first().click().catch(() => undefined);
  await page.waitForTimeout(2500);
  const label = page.getByText(/From connector /u).first();
  await label.waitFor({ timeout: 30000 });
  await label.scrollIntoViewIfNeeded();
  await shot("from-connector-label");
  console.log(JSON.stringify({ labels: await page.getByText(/From connector /u).allInnerTexts() }));
}
