// Connector logos (apps ledger, "Connector logos (LG)"): every connector and plugin row in
// Discover, the agent-load card and a conversation's Add plugin picker show a logo, the
// one for the page's theme, and no monogram. Run with --theme light and --theme dark.
//
//   LOGOS_MODE       discover | card | picker
//   LOGOS_WORKSPACE  workspace name for card and picker (default "logos")
//   LOGOS_AGENT      the agent whose load card to show (card; default clio/earthscope)
//
// Prints one JSON line: each logo slot's row text, which variant shows, whether its image
// loaded, and the monograms (should be none).
import { api, openConversation } from "./_lib.mjs";

const settle = (page, ms = 1500) => page.waitForTimeout(ms);

/** Every logo or monogram inside `scope`: the visible one per row, and whether it loaded. */
async function slots(page, scope) {
  return page.locator(scope).evaluateAll((roots) =>
    roots.flatMap((root) =>
      [...root.querySelectorAll('[data-slot="connector-logo"], [data-slot="monogram"]')]
        .filter((el) => getComputedStyle(el).display !== "none")
        .map((el) => {
          // The row's name: the first text of the nearest ancestor that has any.
          let row = el.parentElement;
          while (row && !(row.innerText || "").trim()) row = row.parentElement;
          return {
            row: (row?.innerText || "").trim().split("\n")[0].slice(0, 40),
            kind: el.dataset.slot,
            variant: el.dataset.themeVariant || "one",
            loaded: el.tagName === "IMG" ? el.complete && el.naturalWidth > 0 : null,
            size: Math.round(el.getBoundingClientRect().width),
          };
        }),
    ),
  );
}

function summary(rows) {
  return {
    logos: rows.filter((r) => r.kind === "connector-logo").length,
    notLoaded: rows.filter((r) => r.kind === "connector-logo" && !r.loaded).map((r) => r.row),
    monograms: rows.filter((r) => r.kind === "monogram").map((r) => r.row),
    variants: rows.filter((r) => r.variant !== "one").map((r) => `${r.row}: ${r.variant}`),
  };
}

export default async function (page, shot) {
  const mode = process.env.LOGOS_MODE || "discover";
  const wide = (page.viewportSize()?.width ?? 1440) > 700;
  const report = { mode, wide };
  if (mode === "discover") {
    if (wide) await page.getByRole("link", { name: "Settings" }).first().click();
    else await page.goto(new URL("/settings/plugins", page.url()).toString());
    await settle(page);
    const list = page.locator('[role="group"][aria-label="Extensions"]').first();
    if (!(await list.waitFor({ timeout: 15000 }).then(() => true, () => false)))
      await page.getByRole("link", { name: /^Extensions/u }).first().click();
    await page.locator('[role="radio"]:visible', { hasText: /^Discover$/u }).first().click();
    await page.locator('[data-slot="discover-group"]').first().waitFor({ timeout: 30000 });
    await settle(page, 3000);
    const groups = page.locator('[data-slot="discover-group"]');
    const count = await groups.count();
    await shot("discover-0");
    // One shot per screen: scroll each later group's top into view.
    let shotAt = 0;
    for (let i = 1; i < count; i += 1) {
      const box = await groups.nth(i).boundingBox();
      const prev = await groups.nth(shotAt).boundingBox();
      if (!box || !prev || box.y - prev.y < (page.viewportSize()?.height ?? 900) * 0.6) continue;
      await groups.nth(i).evaluate((el) => el.scrollIntoView({ block: "start" }));
      await settle(page, 1200);
      await shot(`discover-${i}`);
      shotAt = i;
    }
    await groups.last().evaluate((el) => el.scrollIntoView({ block: "end" }));
    await settle(page, 1200);
    await shot("discover-end");
    report.groups = count;
    report.rows = await slots(page, '[data-slot="discover-group"]');
    report.summary = summary(report.rows);
  } else if (mode === "card") {
    const agent = process.env.LOGOS_AGENT || "clio/earthscope";
    const { session, workspace } = await openConversation(page, process.env.LOGOS_WORKSPACE || "logos");
    report.session = session.id;
    report.workspace = workspace.id;
    await api("PATCH", `/v1/sessions/${session.id}/agent`, { ref: agent });
    await page.reload({ waitUntil: "domcontentloaded" });
    const card = page.locator('[data-intent="agent_needs"]').first();
    await card.waitFor({ timeout: 45000 });
    await settle(page, 3000);
    await card.scrollIntoViewIfNeeded();
    await shot("agent-load-card");
    report.text = (await card.innerText()).replace(/\s+/gu, " ").trim().slice(0, 300);
    report.rows = await slots(page, '[data-intent="agent_needs"]');
    report.summary = summary(report.rows);
  } else if (mode === "picker") {
    const { session, workspace } = await openConversation(page, process.env.LOGOS_WORKSPACE || "logos");
    report.session = session.id;
    report.workspace = workspace.id;
    const lists = page.getByRole("toolbar", { name: "Workbench lists" });
    const box = await lists.boundingBox().catch(() => null);
    if (!box || box.x + box.width > page.viewportSize().width) {
      await page.getByRole("button", { name: "Open workspace canvas" }).first().click();
      await settle(page);
    }
    await lists.getByRole("button", { name: /^Agent/u }).first().click();
    await settle(page, 2000);
    await page.locator('[data-agent-use="tools"]').first().click();
    await settle(page, 2500);
    await page.getByRole("button", { name: /Add (package|plugin)/u }).first().click();
    await settle(page, 2500);
    await shot("add-plugin");
    // The picker is the only place on this page that shows connector logos.
    report.rows = await slots(page, "body");
    report.summary = summary(report.rows);
    await page.locator('[data-slot="connector-logo"]:visible').first().evaluate((el) => {
      let scroller = el.parentElement;
      while (scroller && scroller.scrollHeight <= scroller.clientHeight + 4) scroller = scroller.parentElement;
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    });
    await settle(page, 1200);
    await shot("add-plugin-end");
  }
  console.log(JSON.stringify(report));
}
