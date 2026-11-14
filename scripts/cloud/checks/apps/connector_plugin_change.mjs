// Check (AQ10, CN10 *Amended*): a connector reaches a conversation only when the person
// adds it there. In CHECK_SESSION's Agent tab ("What it can use") this opens "Add plugin",
// reads the Connectors group, and, with CHECK_ACTION:
//   look    only report (default);
//   add     pick CHECK_CONNECTOR (its name) from the picker;
//   remove  remove CHECK_CONNECTOR's package row from this conversation.
// CHECK_TOGGLE ("Turn off" or "Enable") presses the connector's Extensions card button
// first. A change in a conversation with history is applied as CHECK_APPLY (default the
// plugin-change note). Prints one JSON line: the plugin-change notice, the packages listed, whether the
// picker offers the connector and its state words there.
//   CHECK_SESSION (sess_...), CHECK_WORKSPACE (its workspace), CHECK_CONNECTOR (name)
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = async (locator) =>
  ((await locator.count()) ? await locator.first().innerText().catch(() => "") : "").replace(
    /\s+/gu,
    " ",
  );

async function openAgentUse(page, workspace, session) {
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await settle(page, 5000);
  // The canvas keeps its open or closed state between visits: open it only if closed.
  const open = page.getByRole("button", { name: "Open workspace canvas" });
  if (await open.count()) {
    await open.first().click();
    await settle(page, 2500);
  }
  await page
    .getByRole("toolbar", { name: "Workbench lists" })
    .waitFor({ state: "visible", timeout: 15000 })
    .catch(() => undefined);
  const lists = page.getByRole("toolbar", { name: "Workbench lists" });
  console.error("[check] opening the Agent tab");
  await lists.getByRole("button", { name: /^Agent/u }).first().click();
  await settle(page, 2000);
  // The Agent tab remembers its drill-in: open "What it can use" only when not there.
  const tools = page.locator('[data-agent-use="tools"]');
  if (await tools.count()) await tools.first().click();
  await settle(page, 2500);
}

// A change in a conversation with history asks how it reaches the model: CHECK_APPLY
// (default "Yes, as an injection", the plugin-change note).
async function applyMode(page, shot) {
  const ask = page.getByText("Do you want to proceed?");
  await ask.waitFor({ timeout: 5000 }).catch(() => undefined);
  if (!(await ask.count())) return;
  await shot("apply-mode");
  const choice = process.env.CHECK_APPLY || "Yes, as an injection";
  await page.getByRole("button", { name: new RegExp(`^${choice}`, "u") }).first().click();
}

export default async function (page, shot) {
  const session = process.env.CHECK_SESSION;
  const workspace = process.env.CHECK_WORKSPACE || "ws_default";
  const name = process.env.CHECK_CONNECTOR || "ParaView";
  const action = process.env.CHECK_ACTION || "look";
  const report = { action };
  if (process.env.CHECK_TOGGLE) {
    await page.goto(new URL("/settings/plugins", page.url()).toString());
    await page
      .locator('[role="group"][aria-label="Extensions"]')
      .getByText(name, { exact: true })
      .first()
      .click();
    const card = page.locator('[data-slot="connector-setup-card"]');
    await card.waitFor({ timeout: 20000 });
    await card.getByRole("button", { name: process.env.CHECK_TOGGLE, exact: true }).click();
    await settle(page, 8000);
    await shot("connector-card");
    report.card = (await words(card)).slice(0, 400);
  }
  await openAgentUse(page, workspace, session);
  await shot("agent-before");
  const add = page.getByRole("button", { name: /Add (package|plugin)/u }).first();
  console.error("[check] opening Add plugin");
  await add.click();
  await settle(page, 1500);
  const group = page.getByRole("group", { name: "Connectors" });
  report.pickerConnectors = await words(group);
  report.pickerOffers = report.pickerConnectors.includes(name);
  await shot("plugin-picker");
  if (action === "add" && report.pickerOffers) {
    await group.getByRole("option", { name: new RegExp(name, "u") }).first().click();
    await settle(page, 1500);
    await applyMode(page, shot);
    await settle(page, 6000);
  } else {
    await page.keyboard.press("Escape");
  }
  if (action === "remove") {
    console.error("[check] removing");
    await page
      .getByRole("button", { name: new RegExp(`^Remove connector-|^Remove ${name}`, "iu") })
      .first()
      .click()
      .catch(() => undefined);
    await settle(page, 1500);
    // A removal asks to confirm what leaves with it.
    await applyMode(page, shot);
    // In a conversation with no messages the confirmation is a plain Yes / No dialog.
    const yes = page.getByRole("dialog").getByRole("button", { name: /^Yes$/ });
    if (await yes.waitFor({ timeout: 8000 }).then(() => true, () => false)) {
      await shot("remove-confirm");
      await yes.click();
    }
    await settle(page, 6000);
  }
  const notice = page.getByRole("region", { name: "Plugin changes waiting for compaction" });
  await notice.waitFor({ timeout: 15000 }).catch(() => undefined);
  report.notice = (await notice.count()) ? await words(notice) : null;
  report.packages = await page
    .locator("[data-agent-package]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-agent-package")));
  await shot("agent-after");
  console.error(JSON.stringify(report));
}
