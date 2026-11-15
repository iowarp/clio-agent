// CAD tools in the 3D file view (ledger "CAD tools (CAD2, stack N=36)"): measure, exploded
// view, section, part selection, orthographic and inches, axis gizmo.
//
//   CAD_SESSION=/workspaces/<ws>/sessions/<sid>   the conversation whose workspace has the files
//   CAD_FILE=.step                                  which file to open from Files (name fragment)
//   CAD_WORKSPACE=print-lab                         the workspace's name (its crumb opens Files)
//   CHECK_CAD_TOOLS=desktop|phone                   desktop runs every tool with hero shots in
//                                                   light and dark; phone the narrow sheet
// The measurement targets are found by hovering: the snap indicator names what is under the
// pointer (Vertex, Edge, Face), so the step finds the model's ends, a wall and a round edge
// without knowing the geometry. Prints CAD_TOOLS_RESULT with what each tool reported.

export const result = { log: [] };
const phase = process.env.CHECK_CAD_TOOLS || "desktop";

export const visible = (locator) => locator.filter({ visible: true }).first();
export const view = (page) => visible(page.locator('[data-slot="cad-view"]'));
export const canvasBox = async (page) => view(page).locator('[data-slot="cad-canvas"]').boundingBox();

async function setTheme(page, theme) {
  await page.evaluate((dark) => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.classList.toggle("dark", dark);
  }, theme === "dark");
  await page.waitForTimeout(1200);
}

async function bothThemes(page, shot, name) {
  for (const theme of ["light", "dark"]) {
    await setTheme(page, theme);
    await shot(`${name}-${theme}`);
  }
  await setTheme(page, "light");
}

export async function openFile(page, name) {
  const canvas = page.getByRole("button", { name: "Open workspace canvas" }).first();
  if (page.viewportSize().width < 600 && (await canvas.isVisible().catch(() => false))) {
    await canvas.click();
    await page.waitForTimeout(2000);
  }
  if (!(await page.getByRole("treeitem").count())) {
    const crumbs = page.getByRole("button", { name: process.env.CAD_WORKSPACE || "print-lab", exact: true });
    for (let i = 0; i < (await crumbs.count()); i += 1) {
      const box = await crumbs.nth(i).boundingBox();
      if (box && box.y < 40) await crumbs.nth(i).click();
    }
    await page.getByRole("treeitem").first().waitFor({ timeout: 30000 });
  }
  const row = page.getByRole("treeitem", { name: new RegExp(name.replace(".", "\\."), "u") }).first();
  for (let i = 0; i < 3 && !(await row.count()); i += 1) {
    const folders = page.getByRole("button", { name: /^Expand folder/u });
    if (await folders.count()) await folders.first().click();
    await page.waitForTimeout(1200);
  }
  await row.waitFor({ timeout: 30000 });
  await row.click();
  await view(page).waitFor({ timeout: 90000 });
  await page.waitForTimeout(5000);
}

export async function hero(page) {
  if (await page.locator('[data-slot="sidebar"][data-state="expanded"]').count()) {
    await page.locator('[data-sidebar="trigger"]').first().click();
    await page.waitForTimeout(1000);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.count()) await maximize.click().catch(() => undefined);
  await page.waitForTimeout(2500);
}

export async function toolbar(page, label) {
  const button = visible(page.locator(`[data-slot="file-viewer"] button[aria-label="${label}"]`));
  if (await button.count()) return button.click();
  // A narrow bar folds its tools into its … menu.
  await visible(page.getByRole("button", { name: "File actions" })).click();
  await page.waitForTimeout(500);
  await page.getByRole("menuitemcheckbox", { name: label }).or(page.getByRole("menuitem", { name: label })).first().click();
}

export async function moreRow(page, label) {
  await visible(page.getByRole("button", { name: "File actions" })).click();
  await page.waitForTimeout(500);
  await page.getByRole("menuitemcheckbox", { name: label }).or(page.getByRole("menuitem", { name: label })).first().click();
  await page.waitForTimeout(800);
}

export async function standardView(page, name) {
  await visible(page.getByRole("button", { name: "Standard views" })).click();
  await page.waitForTimeout(400);
  await page.getByRole("menuitem", { name }).click();
  await page.waitForTimeout(800);
}

export async function gizmo(page, axis) {
  // Clicking the side already in view looks from the other side, so start from an angle.
  await standardView(page, "Isometric");
  // An end hidden behind its opposite (looking straight down that axis) is still a button.
  await visible(page.getByRole("button", { name: `View from ${axis}` })).click({ force: true });
  await page.waitForTimeout(800);
}

/** Hover at a canvas point and read the snap indicator ("Vertex", "Edge", "Face" or none). */
export async function snapAt(page, x, y) {
  await page.mouse.move(x, y);
  await page.waitForTimeout(60);
  const snap = page.locator('[data-slot="cad-snap"]');
  return (await snap.count()) ? (await snap.innerText()).trim() : "";
}

/** Snaps along a line across the canvas, at `steps` points. */
export async function scan(page, from, to, steps) {
  const out = [];
  for (let i = 0; i <= steps; i += 1) {
    const x = from[0] + ((to[0] - from[0]) * i) / steps;
    const y = from[1] + ((to[1] - from[1]) * i) / steps;
    out.push({ x, y, snap: await snapAt(page, x, y) });
  }
  return out;
}

const measureItems = (page) => page.locator('[data-slot="cad-measure"] li');
export const panelText = async (page, slot) =>
  (await visible(page.locator(`[data-slot="${slot}"]`)).innerText().catch(() => "")).replace(/\s+/gu, " ");

async function clickAt(page, point) {
  await page.mouse.move(point.x, point.y);
  await page.waitForTimeout(80);
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(250);
}

async function measureLength(page) {
  await standardView(page, "Front");
  const box = await canvasBox(page);
  // The row where the model is widest: its two ends there are the length.
  let best;
  for (const gy of [0.35, 0.45, 0.55, 0.65]) {
    const y = box.y + box.height * gy;
    const onModel = (await scan(page, [box.x + 10, y], [box.x + box.width - 10, y], 120)).filter((p) => p.snap);
    const span = onModel.length ? onModel[onModel.length - 1].x - onModel[0].x : 0;
    if (!best || span > best.span) best = { span, onModel };
  }
  const onModel = best?.onModel ?? [];
  result.lengthScan = { hits: onModel.length, edges: onModel.filter((p) => p.snap !== "Face").length };
  // The model's two ends on that row, found to the pixel: the outline in an orthographic
  // front view is the true extent, whatever snaps there (a filleted end has no edge).
  const refine = async (from, to) => {
    const fine = await scan(page, [from, onModel[0].y], [to, onModel[0].y], Math.abs(to - from));
    return fine.find((p) => p.snap);
  };
  if (!onModel.length) return;
  const first = (await refine(onModel[0].x - 14, onModel[0].x + 2)) ?? onModel[0];
  const lastHit = onModel[onModel.length - 1];
  const last = (await refine(lastHit.x + 14, lastHit.x - 2)) ?? lastHit;
  await clickAt(page, first);
  await clickAt(page, last);
}

/** Separate runs of edge snaps along a scanned line, in order. */
function edgeRuns(line) {
  const runs = [];
  let open = false;
  for (const p of line) {
    const edge = p.snap === "Edge" || p.snap === "Vertex";
    if (edge && open) runs[runs.length - 1].push(p);
    else if (edge) runs.push([p]);
    open = edge;
  }
  return runs;
}

/** The gizmo end that looks straight at the cut face: the kept half's open side. */
export async function cutFacing(page) {
  const panel = visible(page.locator('[data-slot="cad-section"]'));
  const axis = (await panel.locator('[role="radio"][aria-checked="true"]').innerText()).trim();
  return (await panel.innerText()).includes("keeping the side above") ? `−${axis}` : `+${axis}`;
}

/** A wall across the section: the cut outline's outer and inner lines, seen face on. */
export async function measureWall(page, columns = [0.4, 0.3, 0.5, 0.6, 0.25, 0.7]) {
  const box = await canvasBox(page);
  for (const gx of columns) {
    const x = box.x + box.width * gx;
    const runs = edgeRuns(await scan(page, [x, box.y + 20], [x, box.y + box.height - 20], 200));
    result.wallScan = (result.wallScan || []).concat(runs.length);
    if (runs.length < 3) continue;
    // The floor: the outer bottom line and the channel's floor line above it. A snap lands
    // on the nearest line, so the run ends pick the two lines even where their runs touch.
    const bottom = runs[runs.length - 1];
    const above = runs[runs.length - 2];
    await clickAt(page, bottom[bottom.length - 1]);
    await clickAt(page, above[0]);
    return true;
  }
  return false;
}

/** A round edge's diameter: hover a fine grid for edges and click each until one is round. */
async function measureHole(page) {
  await page.getByRole("radio", { name: "Radius" }).click();
  const before = await measureItems(page).count();
  const box = await canvasBox(page);
  for (let gy = 0.1; gy <= 0.9; gy += 0.025)
    for (let gx = 0.08; gx <= 0.92; gx += 0.015) {
      const x = box.x + box.width * gx;
      const y = box.y + box.height * gy;
      if ((await snapAt(page, x, y)) !== "Edge") continue;
      await page.mouse.click(x, y);
      await page.waitForTimeout(150);
      if ((await measureItems(page).count()) > before) return true;
    }
  return false;
}

async function desktop(page, shot) {
  await hero(page);
  await shot("open");

  // Measure: the whistle's length, a wall (across a section) and a hole.
  await toolbar(page, "Measure");
  await page.waitForTimeout(500);
  // Orthographic: the outline in a front view is the model's true extent.
  await moreRow(page, "Orthographic");
  await measureLength(page);
  await moreRow(page, "Orthographic");
  await page.mouse.move(720, 20);
  result.length = await panelText(page, "cad-measure");
  await bothThemes(page, shot, "measure-length");
  await toolbar(page, "Section");
  await page.waitForTimeout(600);
  await gizmo(page, await cutFacing(page));
  // Orthographic, so the cut outline and the edges behind it line up and the nearer wins.
  await moreRow(page, "Orthographic");
  result.wallFound = await measureWall(page);
  await moreRow(page, "Orthographic");
  await page.mouse.move(720, 20);
  result.wall = await panelText(page, "cad-measure");
  await bothThemes(page, shot, "measure-wall");
  await toolbar(page, "Section");
  // The hole: select the part with it, isolate it and look at it.
  const plug = page.locator('[data-slot="cad-parts"] button[title^="Select"]').last();
  if (await plug.count()) {
    await plug.click();
    await page.getByRole("radio", { name: "Isolate" }).click();
    await page.waitForTimeout(500);
  }
  for (const side of [null, "+Z", "+Y", "+X"]) {
    if (side) await gizmo(page, side);
    else await standardView(page, "Isometric");
    await toolbar(page, "Fit to view");
    result.hole = await measureHole(page);
    if (result.hole) break;
  }
  await page.mouse.move(720, 20);
  await shot("measure-hole");
  if (await plug.count()) {
    await page.getByRole("radio", { name: "Show all" }).click();
    await page.locator('[data-slot="cad-selection"] button[aria-label^="Close"]').click();
  }
  await standardView(page, "Isometric");
  result.measurements = await panelText(page, "cad-measure");
  await bothThemes(page, shot, "measure");
  // Escape cancels a half-made measurement.
  await page.getByRole("radio", { name: "Distance" }).click();
  const box = await canvasBox(page);
  await clickAt(page, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
  result.pendingHint = await panelText(page, "cad-measure");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  result.afterEscape = await panelText(page, "cad-measure");
  // Clear the measurements so the other tools' shots show the model alone.
  await visible(page.locator('[data-slot="cad-measure"] button').filter({ hasText: /^Clear/u })).click();
  result.afterClear = await panelText(page, "cad-measure");
  await toolbar(page, "Measure");

  // Exploded view: opens part-way, then the slider moves it.
  await toolbar(page, "Explode");
  await page.waitForTimeout(1200);
  result.explode = await panelText(page, "cad-explode");
  await bothThemes(page, shot, "explode");
  const thumb = visible(page.locator('[data-slot="cad-explode"] [role="slider"]'));
  await thumb.focus();
  for (let i = 0; i < 25; i += 1) await page.keyboard.press("ArrowRight");
  await page.waitForTimeout(600);
  result.explodeMore = await panelText(page, "cad-explode");
  await shot("explode-more");
  await toolbar(page, "Explode");
  await page.waitForTimeout(1000);

  // Section through the middle, seen from the kept side.
  await toolbar(page, "Section");
  await page.waitForTimeout(800);
  result.section = await panelText(page, "cad-section");
  await shot("section-iso");
  await gizmo(page, await cutFacing(page));
  await bothThemes(page, shot, "section");
  await page.getByRole("button", { name: "Flip", exact: true }).click();
  await page.waitForTimeout(600);
  await gizmo(page, await cutFacing(page));
  result.sectionFlipped = await panelText(page, "cad-section");
  await shot("section-flipped");
  await toolbar(page, "Section");
  await standardView(page, "Isometric");

  // Select a part by clicking it, then ghost and isolate the others.
  const pick = await canvasBox(page);
  await page.mouse.click(pick.x + pick.width / 2, pick.y + pick.height / 2);
  await page.waitForTimeout(600);
  result.selected = await panelText(page, "cad-selection");
  if (result.selected) {
    await shot("select");
    const ghost = page.getByRole("radio", { name: "Ghost others" });
    if (await ghost.count()) {
      await ghost.click();
      await page.waitForTimeout(600);
      await bothThemes(page, shot, "select-ghost");
      await page.getByRole("radio", { name: "Isolate" }).click();
      await page.waitForTimeout(600);
      await shot("select-isolate");
      await page.getByRole("radio", { name: "Show all" }).click();
    }
    await page.keyboard.press("Escape");
  }

  // Orthographic and inches.
  await moreRow(page, "Orthographic");
  await moreRow(page, "Inches");
  await toolbar(page, "Fit to view");
  result.facts = (await visible(page.locator('[data-slot="file-viewer"] [role="toolbar"]')).innerText().catch(() => "")).replace(/\s+/gu, " ");
  await shot("ortho-inch");
  await visible(page.getByRole("button", { name: "File actions" })).click();
  await page.waitForTimeout(500);
  result.moreMenu = (await page.getByRole("menu").first().innerText().catch(() => "")).split("\n");
  await shot("more-menu");
  await page.keyboard.press("Escape");
  await moreRow(page, "Inches");
  await moreRow(page, "Orthographic");
}

async function phone(page, shot) {
  await shot("open");
  await toolbar(page, "Measure");
  await page.waitForTimeout(600);
  const box = await canvasBox(page);
  // Two taps across the middle of the model.
  for (const [gx, gy] of [
    [0.3, 0.42],
    [0.8, 0.5],
  ])
    await page.touchscreen.tap(box.x + box.width * gx, box.y + box.height * gy);
  await page.waitForTimeout(600);
  result.phoneMeasure = await panelText(page, "cad-measure");
  await shot("measure");
  await toolbar(page, "Measure");
  await toolbar(page, "Section");
  await page.waitForTimeout(600);
  result.phoneSection = await panelText(page, "cad-section");
  await shot("section");
  await toolbar(page, "Section");
  await toolbar(page, "Explode");
  await page.waitForTimeout(1200);
  await shot("explode");
  await toolbar(page, "Explode");
}

export default async function (page, shot) {
  await page.goto(new URL(process.env.CAD_SESSION, page.url()).toString());
  await page.waitForTimeout(8000);
  await openFile(page, process.env.CAD_FILE || ".step");
  if (phase === "phone") await phone(page, shot);
  else await desktop(page, shot);
  console.log(`CAD_TOOLS_RESULT ${JSON.stringify(result)}`);
}
