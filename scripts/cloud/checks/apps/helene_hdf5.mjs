// The Helene HDF5 demo as a person would work it: one plain request per turn, no tool named.
//
//   WS=<workspace> SID=<session> node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_hdf5.mjs --label helene --width 1600 --height 1000
//
// The workspace holds era5/msl_2024-09.nc, era5/sst_2024-09.nc and ERA5's cloud, rain and
// snow water for 24-27 September (bench/live/setup_ws.py with NO_S3=1). The person asks what is in the
// pressure file (the file @-referenced from the composer), opens the field from the answer,
// reads its attributes and looks at it, adds /MSL from the viewer and asks to track the
// storm; then asks how it deepened with /SSTK @-referenced from the composer (Tab into the
// file); then the clouds in 3D; then a dashboard to share. No turn names a tool.
import { showCursor } from "./_cursor.mjs";
import { api, send, waitIdle } from "./_lib.mjs";

const MSL = "msl_2024-09.nc";
const SST = "sst_2024-09.nc";
const TURNS = {
  contents: "What's in here?",
  track:
    "Hurricane Helene crossed the Gulf in late September 2024. Can you track its centre and " +
    "central pressure in this from the 24th to the 27th?",
  deepen: "How fast did it deepen, and was the water under it warm enough to explain that?",
  // The person names the viewer and the look they want (a see-through volume, as in a
  // published hurricane figure), and the fields: this turn is person-directed, so it is
  // evidence of the mechanism and the agent's analysis, not of its choice of view.
  clouds:
    "What did the clouds around it look like on the 26th, before it came ashore? The cloud, " +
    "rain and snow water for those days are in the era5 folder. Show it to me in ParaView " +
    "from above, tilted a little, so I can see the spiral. Give the cloud droplets, ice, rain " +
    "and snow each their own strong colour, and hide the thin wisps so the shape of the " +
    "storm stands out.",
  share: "Put all of this on a dashboard so I can share it with colleagues.",
  follow: "Can I step through the days on this and have everything follow, the 3D clouds too?",
  go: "3-hourly, go ahead.",
  beat:
    "I stepped the 3D view to landfall and raised the liquid-water surface. What does it " +
    "show now, and how does it compare with the day before?",
  beatTurned:
    "I stepped the 3D view to landfall and turned the storm around. What does it show now, " +
    "and how does it compare with the day before?",
};

const T0 = Date.now();
/** A timestamp from the start of the recording, for cutting the video into beats. */
function mark(name) {
  console.log(`MARK ${name} ${((Date.now() - T0) / 1000).toFixed(1)}`);
}

async function settle(page, sid, log, shot, name, minutes = 25) {
  mark(`${name}-waiting`);
  // The agent may offer a connector (ParaView) for this conversation: the person reads the
  // offer and clicks Add, as anyone would to get the view they asked for. It is watched on
  // its own, so the click follows the card within a second instead of a polling round.
  let settled = false;
  const watcher = (async () => {
    while (!settled) {
      await acceptOffers(page, shot, name).catch(() => {});
      await page.waitForTimeout(300).catch(() => {});
    }
  })();
  try {
    let n = 0;
    const until = Date.now() + minutes * 60_000;
    while (Date.now() < until) {
      const done = await waitIdle(page, sid, log, 0.5);
      n += 1;
      if (n % 6 === 0 || done) await shot(`${name}-${String(n).padStart(2, "0")}`);
      if (done) {
        await declineOptionalPlugins(page);
        await toLatest(page);
        mark(`${name}-answered`);
        return true;
      }
    }
    mark(`${name}-timeout`);
    return false;
  } finally {
    settled = true;
    await watcher;
  }
}

/**
 * The agent's own optional plugins (an offer that says the agent works without them): the
 * person answers Not now, so the card does not stay open over the rest of the walkthrough.
 */
async function declineOptionalPlugins(page) {
  const card = page.locator("[data-interaction-kind]").filter({ hasText: /works without (it|them)/ });
  const notNow = card.getByRole("button", { name: /^Not now/ }).locator("visible=true").first();
  if (!(await notNow.isVisible().catch(() => false))) return;
  await page.waitForTimeout(800);
  await notNow.click({ timeout: 5000 }).catch(() => {});
  console.log("DECLINED optional plugins");
}

async function acceptOffers(page, shot, name) {
  const offer = page.locator('[data-interaction-kind="connector_setup"]').locator("visible=true");
  const add = offer.getByRole("button").filter({ hasText: /Add/ }).first();
  if (!(await add.isVisible().catch(() => false))) return;
  await add.scrollIntoViewIfNeeded();
  mark(`${name}-offer-shown`);
  await page.waitForTimeout(1000); // reads what it adds and why
  await add.click({ timeout: 5000 });
  mark(`${name}-offer-accepted`);
  console.log("ACCEPTED connector offer");
}

/** Reference a file from the composer's @ menu; with `dataset`, Tab into it and pick one. */
async function atReference(page, file, dataset) {
  await page.keyboard.type(` @${file.slice(0, 3)}`, { delay: 70 });
  const option = page.getByRole("option").filter({ hasText: file }).first();
  await option.waitFor();
  if (!dataset) return option.click();
  await page.keyboard.press("Tab");
  const inner = page.getByRole("option").filter({ hasText: new RegExp(`^/${dataset}`) }).first();
  await inner.waitFor();
  await page.waitForTimeout(800);
  await inner.click();
}

/** The person opens the field from the answer: a dataset link, else the file and its tree. */
async function openFieldFromAnswer(page, shot) {
  const turn = page.locator("main").last();
  const field = turn.getByRole("link", { name: /^MSL$/ }).last();
  if (await field.count()) {
    await field.click();
    console.log("FIELD_OPENED_BY", "dataset link");
  }
  else {
    // No dataset link in the answer: the file link, else the file chip on the person's message.
    const file = turn.getByRole("link", { name: new RegExp(MSL.replace(".", "\\.")) }).last();
    if (await file.count()) await file.click();
    else await turn.getByRole("button", { name: new RegExp(MSL.replace(".", "\\.")) }).first().click();
    console.log("FIELD_OPENED_BY", (await file.count()) ? "file link" : "message chip");
    await page.waitForTimeout(5000);
  }
  const viewer = page.frameLocator('iframe[title^="HDF5 viewer"]');
  // Each control is in the file's toolbar or inside the viewer; a person finds it in
  // a moment or not at all, so a missing one is logged, never waited on for 30 s.
  const press = async (name) => {
    for (const where of [page, viewer]) {
      const button = where.getByRole("button", { name, exact: true }).first();
      if (await button.isVisible({ timeout: 1500 }).catch(() => false)) {
        await button.click({ timeout: 3000 });
        return true;
      }
    }
    console.log(`STEP_SKIPPED ${name}`);
    return false;
  };
  // Opened on the file: show its tree and choose the field. A file with one field opens
  // on that field, with no tree to open.
  if (!(await field.count()) && (await press("Explorer"))) {
    await viewer.getByText("MSL", { exact: true }).first().click({ timeout: 5000 });
    await page.waitForTimeout(1500);
    // In the docked canvas the tree covers the viewer: close it to see the field.
    await press("Explorer");
  }
  await page.waitForTimeout(4000);
  await shot("field-open");
  // Reads what the field is, then looks at it.
  await press("Attributes");
  await page.waitForTimeout(4000);
  await shot("field-attributes");
  await press("Image");
  await page.waitForTimeout(5000);
  // ERA5 stores latitude north to south, so the raw array draws south up: flip it.
  if (await press("More controls")) await press("Flip Y");
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(1500);
  // Scrubs time to the storm in the Gulf (26 Sep 12Z is step 612 of 720).
  const slider = viewer.getByRole("slider").last();
  const box = await slider.boundingBox({ timeout: 3000 }).catch(() => null);
  if (!box) console.log("STEP_SKIPPED time slider");
  if (box) {
    await page.mouse.move(box.x + 2, box.y + box.height / 2);
    await page.mouse.down();
    // Measured: the track is not the box; 0.75 of it is step ~612 (26 Sep 12Z).
    await page.mouse.move(box.x + 2 + (box.width - 2) * 0.7516, box.y + box.height / 2, { steps: 25 });
    await page.mouse.up();
    // The track's share of the box depends on the panel's width: finish with the arrow
    // keys on the focused slider, as a person nudging it would.
    for (let k = 0; k < 120; k += 1) {
      // A range input: its value, else an ARIA slider's.
      const now = Number(
        await slider
          .evaluate((el) => ("value" in el ? el.value : el.getAttribute("aria-valuenow")))
          .catch(() => NaN),
      );
      if (!Number.isFinite(now) || now === 612) break;
      await page.keyboard.press(now < 612 ? "ArrowRight" : "ArrowLeft");
      await page.waitForTimeout(40);
    }
    await page.waitForTimeout(4000);
  }
  await shot("field-image");
}

/** The person's own turn in the dashboard: step the 3D view, raise a surface, then ask. */
async function finalBeat(page, shot, sid, log) {
  mark("beat-dashboard");
  // The dashboard may already be open (the agent shows what it published); otherwise the
  // newest dashboard card in the conversation opens it in the canvas.
  // Several dashboard tabs may be open; only the shown one's live view counts.
  const live = page.locator('iframe[title*="ParaView"]').locator("visible=true");
  // A dashboard the agent just published may still be mounting its live view.
  await live.first().waitFor({ timeout: 20000 }).catch(() => {});
  if (!(await live.count())) {
    await toLatest(page);
    const label = page.getByText("Dashboard", { exact: true }).last();
    await label.scrollIntoViewIfNeeded();
    await label.locator("xpath=../preceding-sibling::*[1]").click();
  }
  await page.locator('[data-slot="a2ui-surface-root"]').last().waitFor({ timeout: 60000 });
  await page.getByRole("button", { name: "Maximize canvas" }).click({ timeout: 3000 }).catch(() => {});
  const pv = page.locator('iframe[title*="ParaView"]').locator("visible=true").first();
  await pv.scrollIntoViewIfNeeded();
  await page.waitForTimeout(12000);
  // The live view's page sits inside the app frame's own sandbox frame.
  const outer = await (await pv.elementHandle())?.contentFrame();
  const inside = (f) => [f, ...f.childFrames().flatMap((c) => inside(c))];
  const app = outer && inside(outer).find((f) => f.url().endsWith("/index.html"));
  if (!app) throw new Error("the live 3D view did not load in the dashboard");
  // Rendered on the ParaView machine, the view keeps drawing when the canvas grows.
  await app.getByRole("button", { name: "Render on ParaView machine" }).click();
  await page.waitForTimeout(5000);
  await shot("beat-dashboard");
  // Step the 3D view to landfall (27 Sep 03Z is step 26 of 32); the other views follow.
  // A narrow view folds the step buttons into a menu on its step counter; open it then.
  const next = app.getByRole("button", { name: "Next step" }).locator("visible=true").first();
  if (!(await next.isVisible().catch(() => false))) {
    await app.getByRole("button", { name: /^Time step \d+ of \d+$/ }).first().click();
    await page.waitForTimeout(600);
  }
  const back = app.getByRole("button", { name: "Previous step" }).locator("visible=true").first();
  // The counter updates after the server moves the view, so step and wait for it.
  // Landfall (27 Sep 03Z) is step 26 of a 3-hourly 24-27 Sep series: the same share of
  // whatever series the agent built.
  const { total } = await stepOf(app);
  const target = Math.max(1, Math.round((total * 26) / 32));
  for (let k = 0; k < 40; k += 1) {
    const { now } = await stepOf(app);
    if (now === target) break;
    await (now < target ? next : back).click({ timeout: 5000 });
    await page.waitForTimeout(1200);
  }
  await page.keyboard.press("Escape");
  await page.waitForTimeout(6000);
  await shot("beat-landfall");
  // The person's own change in the view: raise the liquid-water surface when the scene has
  // one (an isosurface), else turn the storm around to look at it (a volume).
  const liquid = app.getByRole("button", { name: /^(?!Hide |Show |Remove ).*(liquid|droplet)/i }).first();
  if (!(await liquid.isVisible().catch(() => false))) {
    await app.getByRole("button", { name: "Pipeline" }).click();
    await page.waitForTimeout(1200);
  }
  let question = TURNS.beat;
  if (await liquid.isVisible().catch(() => false)) {
    await liquid.click();
    // Add filter is a toggle too: open it only when its Edit button is not showing.
    const edit = app.getByRole("button", { name: /^Edit/ }).first();
    if (!(await edit.isVisible().catch(() => false))) {
      await app.getByRole("button", { name: "Add filter" }).click();
    }
    await edit.click();
    // Drags its first value's slider right (a log slider when the field spans decades);
    // release applies it.
    const values = app.getByRole("textbox", { name: "Values" });
    const before = await values.inputValue();
    const thumb = app.getByRole("slider", { name: /Values 1 slider/ });
    const box = await thumb.boundingBox();
    if (!box) throw new Error("the isovalue slider is not shown");
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + box.width / 2, y);
    await page.mouse.down();
    // About two thirds of a decade on a log slider: the surface rises ~2.5x, still visible.
    await page.mouse.move(box.x + box.width / 2 + 40, y, { steps: 30 });
    await page.waitForTimeout(400);
    await page.mouse.up();
    await page.waitForTimeout(1500);
    const after = await values.inputValue();
    if (after === before) throw new Error(`dragging the slider left the values at ${before}`);
    console.log(`DRAGGED ${before} -> ${after}`);
    await page.waitForTimeout(8000);
    await shot("beat-raised");
    await app.getByRole("button", { name: "Done" }).click({ timeout: 3000 }).catch(() => {});
  } else {
    // A volume: close the drawer and turn the storm around with the mouse.
    await app.getByRole("button", { name: "Pipeline" }).click({ timeout: 3000 }).catch(() => {});
    const view = await pv.boundingBox();
    if (!view) throw new Error("the 3D view is not shown");
    const cx = view.x + view.width * 0.55;
    const cy = view.y + view.height * 0.6;
    await page.mouse.move(cx, cy, { steps: 15 });
    await page.mouse.down();
    await page.mouse.move(cx + 220, cy - 60, { steps: 60 });
    await page.mouse.up();
    await page.waitForTimeout(3000);
    console.log("ROTATED the 3D view");
    await shot("beat-turned");
    question = TURNS.beatTurned;
  }
  await page.getByRole("button", { name: "Restore canvas beside conversation" }).click({ timeout: 5000 }).catch(() => {});
  if (process.env.BEAT_ASK === "0") return;
  await send(page, question);
  await settle(page, sid, log, shot, "beat", 15);
}

/**
 * The person looks at the dashboard the agent just published: full width, scrolled slowly
 * from top to bottom with a pause on each screen, then back beside the conversation.
 */
export async function showOffDashboard(page, shot, name) {
  const surface = page.locator('[data-slot="a2ui-surface-root"]').locator("visible=true").last();
  if (!(await surface.isVisible({ timeout: 15000 }).catch(() => false))) {
    console.log(`STEP_SKIPPED dashboard ${name}: none on screen`);
    return;
  }
  mark(`${name}-dashboard`);
  await page.getByRole("button", { name: "Maximize canvas" }).click({ timeout: 5000 });
  await page.waitForTimeout(3500);
  await shot(`${name}-dashboard-top`);
  const box = await surface.boundingBox();
  if (box) await page.mouse.move(box.x + box.width / 2, Math.min(box.y + 300, 700), { steps: 20 });
  // The scroller is the surface's nearest scrollable ancestor; step it a screen at a time.
  const scroll = () =>
    surface.evaluate((node) => {
      let el = node;
      while (el && !(el.scrollHeight > el.clientHeight + 4 && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
      return el ? [el.scrollTop, el.scrollHeight - el.clientHeight] : [0, 0];
    });
  for (let k = 0; k < 12; k += 1) {
    const [top, end] = await scroll();
    if (top >= end - 2) break;
    await page.mouse.wheel(0, 420);
    await page.waitForTimeout(2200);
  }
  await page.waitForTimeout(2000);
  await shot(`${name}-dashboard-end`);
  await page.getByRole("button", { name: "Restore canvas beside conversation" }).click({ timeout: 5000 });
  await page.waitForTimeout(1500);
  await toLatest(page);
}

/** Whether the agent's latest reply ends by asking the person something. */
async function lastAnswerAsks(sid) {
  const exported = await api("GET", `/v1/sessions/${sid}/export`);
  const replies = (exported.messages || []).filter((m) => m.role === "assistant");
  const parts = replies.at(-1)?.parts || [];
  const text = parts.filter((p) => p.type === "text").map((p) => p.text || "").join(" ").trim();
  // A question in the closing paragraph counts, even when a cost note follows it.
  const closing = text.split(/\n\s*\n/).filter((p) => p.trim()).at(-1) || "";
  return /\?/.test(closing);
}

/** Back to the newest message after a long answer, as a person reading along would. */
async function toLatest(page) {
  const box = await page.getByRole("log", { name: "Conversation" }).first().boundingBox()
    .catch(() => null) ?? await page.locator('[aria-label="Conversation"]').first().boundingBox()
    .catch(() => null);
  if (!box) return;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let k = 0; k < 6; k += 1) {
    await page.mouse.wheel(0, 4000);
    await page.waitForTimeout(150);
  }
}

async function stepOf(app) {
  const text = await app.getByText(/^\s*\d+\s*\/\s*\d+\s*$/).first().textContent().catch(() => "0/0");
  const [now, total] = text.split("/").map((v) => Number(v.trim()));
  return { now, total };
}

export default async function (page, shot) {
  const ws = process.env.WS;
  const sid = process.env.SID;
  const only = (process.env.TURNS || Object.keys(TURNS).join(",")).split(",");
  const log = [];
  // A recording shows the pointer: headless Chromium draws none.
  if (process.env.SHOW_CURSOR !== "0") await showCursor(page);
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  const composer = page.locator('textarea, [contenteditable="true"]').last();
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await page.waitForTimeout(2000);
  // Only this conversation on screen: the workspace list is hidden for the recording.
  // The branded app with its one workspace, briefly; then the side panel collapses for the
  // rest of the demo. The app remembers its state, so collapse only when it is showing.
  const listShown = await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false);
  if (process.env.HIDE_SIDEBAR !== "0" && listShown) {
    await page.waitForTimeout(2500);
    await page.getByRole("button", { name: "Toggle Sidebar" }).first().click();
    await page.waitForTimeout(800);
  }
  mark("start");
  const chips = () => page.locator('[data-slot="composer-toolbar"]').locator("xpath=..").innerText();

  if (only.includes("contents")) {
    mark("contents-typing");
    await composer.click();
    await atReference(page, MSL);
    await page.keyboard.type(" " + TURNS.contents);
    await shot("contents-request");
    await page.keyboard.press("Enter");
    await settle(page, sid, log, shot, "contents");
  }
  if (only.includes("track")) {
    mark("field-open");
    await openFieldFromAnswer(page, shot);
    mark("field-looked");
    if (process.env.FIELD_ONLY === "1") return;
    await page.getByRole("button", { name: "Add to message" }).click();
    await page.waitForTimeout(800);
    if (!(await chips()).includes("/MSL")) throw new Error("the /MSL reference was not added");
    await composer.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" " + TURNS.track);
    await shot("track-request");
    await page.keyboard.press("Enter");
    await settle(page, sid, log, shot, "track");
  }
  if (only.includes("deepen")) {
    mark("deepen-typing");
    await composer.click();
    await page.keyboard.type(TURNS.deepen);
    await atReference(page, SST, "SSTK");
    await page.waitForTimeout(800);
    if (!(await chips()).includes("/SSTK")) throw new Error("the /SSTK reference was not added");
    await shot("deepen-request");
    await page.keyboard.press("Enter");
    await settle(page, sid, log, shot, "deepen", 20);
  }
  for (const name of ["clouds", "share", "follow", "go"]) {
    if (!only.includes(name)) continue;
    // The person answers only a question: when the agent built the time series without
    // asking how often to step, there is nothing to say yes to.
    if (name === "go" && !(await lastAnswerAsks(sid))) {
      mark("go-skipped");
      continue;
    }
    mark(`${name}-typing`);
    await send(page, TURNS[name]);
    await settle(page, sid, log, shot, name, 35);
    // A published dashboard is shown off before the conversation goes on.
    if (name === "share" || name === "go") {
      await showOffDashboard(page, shot, name).catch((error) =>
        console.log(`STEP_SKIPPED dashboard ${name}: ${String(error).slice(0, 160)}`),
      );
    }
  }
  if (only.includes("beat")) await finalBeat(page, shot, sid, log);
  await shot("final");
  console.log("LOG", JSON.stringify(log));
}
