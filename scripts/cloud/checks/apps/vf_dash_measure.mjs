// View call fixes (VF, MC-F5): a dashboard with a `clio.mcp-app.v1` view beside a table, at
// the docked and the maximized width. Measures whether the view stays inside its cell: the
// view component's box, its cell's (the nearest A2UI frame) and the next sibling's top.
//
//   VF_SESSION  "<workspace id>/<session id>"
//   VF_CARD     text of the dashboard's card in the transcript (a regular expression)
// A capture (capture_a2ui_surface, a region comment) draws the surface with every live
// height frozen; "AsCaptured" measures that layout too (MC-F5 showed only there).
// Prints one JSON line: per width, each view's box, its cell's, the cells it covers and
// whether it spills, and shoots each width.
const sleep = (page, ms) => page.waitForTimeout(ms);

/**
 * The layout a capture draws: the capture clones the surface with every element's live
 * height frozen, so each stacked row's height becomes definite. Freeze them the same way
 * in the live page, measure, then thaw.
 */
async function measureFrozen(page) {
  await page.evaluate(() => {
    for (const row of document.querySelectorAll('[data-slot="a2ui-surface-root"] div')) {
      if (getComputedStyle(row).display !== "flex") continue;
      row.dataset.vfFrozen = row.style.height;
      row.style.height = `${row.getBoundingClientRect().height}px`;
    }
  });
  const frozen = await measure(page);
  await page.evaluate(() => {
    for (const row of document.querySelectorAll("[data-vf-frozen]")) {
      row.style.height = row.dataset.vfFrozen;
      delete row.dataset.vfFrozen;
    }
  });
  return frozen;
}

async function measure(page) {
  return page.evaluate(() => {
    const box = (el) => {
      const r = el.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right) };
    };
    const out = [];
    for (const view of document.querySelectorAll("[data-mcp-app-component]")) {
      if (!view.getClientRects().length) continue;
      const cell = view.closest('[data-slot="a2ui-frame"]') || view.parentElement;
      const mine = box(view);
      // Every other cell of the surface that the view's box covers.
      const covers = [...document.querySelectorAll('[data-slot="a2ui-frame"]')]
        .filter((other) => !other.contains(view) && !view.contains(other) && other.getClientRects().length)
        .map((other) => ({ other, b: box(other) }))
        .filter(({ b }) => b.top < mine.bottom - 1 && b.bottom > mine.top + 1 && b.left < mine.right - 1 && b.right > mine.left + 1)
        .map(({ other, b }) => ({ ...b, text: (other.innerText || "").slice(0, 40) }));
      const iframe = view.querySelector("iframe");
      out.push({ view: mine, frame: iframe ? box(iframe) : null, cell: box(cell), covers,
        spills: mine.bottom > box(cell).bottom + 1 || mine.right > box(cell).right + 1 });
    }
    return out;
  });
}

export default async function (page, shot) {
  const [workspace, session] = process.env.VF_SESSION.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await sleep(page, 8000);
  // The dashboard's card in the transcript opens it in the workbench (its latest version).
  const card = page.getByText(new RegExp(process.env.VF_CARD || "dashboard", "i")).filter({ visible: true });
  await card.last().click({ timeout: 30000 });
  await sleep(page, 20000);
  const docked = await measure(page);
  await shot("docked");
  const results = { docked, dockedAsCaptured: await measureFrozen(page), viewport: page.viewportSize() };
  if (page.viewportSize().width > 800) {
    const toggle = page.getByRole("button", { name: "Toggle Sidebar" }).first();
    if (await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false))
      await toggle.click().catch(() => undefined);
    await page.getByRole("button", { name: "Maximize canvas" }).first().click({ timeout: 10000 }).catch(() => undefined);
    await sleep(page, 12000);
    results.maximized = await measure(page);
    results.maximizedAsCaptured = await measureFrozen(page);
    await shot("maximized-hero");
  }
  console.log(JSON.stringify(results));
  return results;
}
