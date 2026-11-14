// CAD and mesh files in the file viewer (ledger "CAD files in the viewer"). Every request is
// plain: the agent picks how to model, export and show the files.
//
// CHECK_CAD picks the phase:
//   request  a new conversation in the `print-lab` workspace with the plain design request
//            (CAD_REQUEST overrides it); waits for the turn and prints the session and the
//            files written
//   view     CAD_SESSION=/workspaces/<ws>/sessions/<sid>: open the STEP from the Files list
//            in a workbench tab, read its parts and size, hide a part, orbit, choose a
//            standard view, open Export to…, and take the shots (hero shots at desktop width)
//   export   as view, then choose Export to… STL and report the downloaded file
// Prints CAD_RESULT with what it found.
import { api, openConversation, send, waitIdle } from "./_lib.mjs";

const REQUEST =
  process.env.CAD_REQUEST ||
  "Design a loud 3D-printable whistle for a Bambu A1 (256×256×256 mm plate, 0.4 mm nozzle, PLA) " +
    "with CadQuery, as two parts, and export a STEP of the assembly and an STL per part.";

const result = { log: [] };

async function collapseSidebar(page) {
  if (!(await page.locator('[data-slot="sidebar"][data-state="expanded"]').count())) return;
  const trigger = page.locator('[data-sidebar="trigger"]').first();
  if (await trigger.count()) await trigger.click();
  await page.waitForTimeout(1200);
}

/** Open a workspace file from the Files list: the workspace crumb opens it in the workbench. */
async function openFromFiles(page, name) {
  const canvas = page.getByRole("button", { name: "Open workspace canvas" }).first();
  if (page.viewportSize().width < 600 && (await canvas.isVisible().catch(() => false))) {
    await canvas.click();
    await page.waitForTimeout(2000);
  }
  if (!(await page.getByRole("treeitem").count())) {
    const crumbs = page.getByRole("button", { name: "print-lab", exact: true });
    for (let i = 0; i < (await crumbs.count()); i += 1) {
      const box = await crumbs.nth(i).boundingBox();
      if (box && box.y < 40) await crumbs.nth(i).click();
    }
    await page.getByRole("treeitem").first().waitFor({ timeout: 30000 });
  }
  const folder = page.getByRole("button", { name: "Expand folder out" }).first();
  const row = page.getByRole("treeitem", { name: new RegExp(name.replace(".", "\\."), "u") }).first();
  if (!(await row.count()) && (await folder.count())) await folder.click();
  await page.waitForTimeout(1500);
  await row.waitFor({ timeout: 30000 });
  await row.click();
  await page.locator('[data-slot="cad-view"]').first().waitFor({ timeout: 90000 });
  await page.waitForTimeout(5000);
}

async function readView(page) {
  const view = page.locator('[data-slot="cad-view"]').filter({ visible: true }).first();
  const bar = page.locator('[data-slot="file-viewer"]').filter({ visible: true }).first();
  return {
    label: await view.locator('[role="img"]').getAttribute("aria-label"),
    parts: await view.locator('[data-slot="cad-parts"] li').allInnerTexts(),
    bar: (await bar.locator('[role="toolbar"]').first().innerText().catch(() => "")).replace(/\s+/gu, " "),
  };
}

async function orbit(page) {
  const canvas = page.locator('[data-slot="cad-view"] canvas').filter({ visible: true }).first();
  const box = await canvas.boundingBox();
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 120, y - 40, { steps: 10 });
  await page.mouse.up();
  await page.waitForTimeout(800);
}

async function frameMenu(page) {
  await page.getByRole("button", { name: "File actions" }).filter({ visible: true }).first().click();
  await page.waitForTimeout(800);
}

async function hero(page, shot, label) {
  await collapseSidebar(page);
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.count()) await maximize.click().catch(() => undefined);
  await page.waitForTimeout(5000);
  for (const theme of ["light", "dark"]) {
    await page.evaluate((dark) => {
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      document.documentElement.classList.toggle("dark", dark);
    }, theme === "dark");
    await page.waitForTimeout(3000);
    await shot(`${label}-hero-${theme}`);
  }
  const restore = page.getByRole("button", { name: "Restore canvas beside conversation" }).first();
  if (await restore.count()) await restore.click().catch(() => undefined);
}

export default async function (page, shot) {
  const phase = process.env.CHECK_CAD || "request";
  const phone = page.viewportSize().width < 600;
  if (phase === "request") {
    const { session, root } = await openConversation(page, "print-lab");
    result.session = session.id;
    result.root = root;
    await send(page, REQUEST);
    result.settled = await waitIdle(page, session.id, result.log, 40);
    const files = await api("GET", `/v1/workspaces/${session.workspace_id}/files`).catch(() => ({}));
    result.files = (files.entries || []).map((e) => `${e.path} ${e.size ?? ""}`);
    await shot("request-done");
    console.log(`CAD_RESULT ${JSON.stringify(result)}`);
    return;
  }
  await page.goto(new URL(process.env.CAD_SESSION, page.url()).toString());
  await page.waitForTimeout(8000);
  await shot("conversation");
  await openFromFiles(page, process.env.CAD_FILE || ".step");
  await shot("step-open");
  result.opened = await readView(page);
  if (!phone) {
    // Hide the second part, then show it again.
    const boxes = page.locator('[data-slot="cad-parts"] [role="checkbox"]');
    if ((await boxes.count()) > 1) {
      await boxes.nth(1).click();
      await page.waitForTimeout(800);
      result.hidden = await readView(page);
      await shot("part-hidden");
      await boxes.nth(1).click();
    }
  }
  await orbit(page);
  await shot("orbited");
  await page.getByRole("button", { name: "Standard views" }).filter({ visible: true }).first().click().catch(() => undefined);
  await page.waitForTimeout(600);
  await shot("views-menu");
  await page.getByRole("menuitem", { name: "Front" }).click().catch(() => page.keyboard.press("Escape"));
  await page.waitForTimeout(800);
  await shot("front");
  await page.getByRole("button", { name: "Fit to view" }).filter({ visible: true }).first().click().catch(() => undefined);
  await page.getByRole("button", { name: "Standard views" }).filter({ visible: true }).first().click().catch(() => undefined);
  await page.getByRole("menuitem", { name: "Isometric" }).click().catch(() => page.keyboard.press("Escape"));
  await page.waitForTimeout(800);
  // Download ▾ holds the original and Export to… the conversions.
  const download = page.locator('[data-slot="file-viewer"] button[aria-label^="Download"]').filter({ visible: true });
  result.downloadButtons = await download.evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
  const more = page.getByRole("button", { name: /Download options|Export/u }).filter({ visible: true }).first();
  if (await more.count()) {
    await more.click();
    await page.waitForTimeout(800);
    result.exportMenu = (await page.getByRole("menu").first().innerText().catch(() => "")).split("\n");
    await shot("export-menu");
    if (phase === "export") {
      const [file] = await Promise.all([
        page.waitForEvent("download", { timeout: 30000 }),
        page.getByRole("menuitem", { name: /STL/u }).first().click(),
      ]);
      const saved = `${process.env.CLAUDE_PROJECT_DIR}/.cloud-run/cad-export.stl`;
      await file.saveAs(saved);
      result.exported = { name: file.suggestedFilename(), saved };
    } else await page.keyboard.press("Escape");
  }
  await frameMenu(page);
  result.moreMenu = (await page.getByRole("menu").first().innerText().catch(() => "")).split("\n");
  await shot("more-menu");
  await page.keyboard.press("Escape");
  if (!phone) await hero(page, shot, "step");
  console.log(`CAD_RESULT ${JSON.stringify(result)}`);
}
