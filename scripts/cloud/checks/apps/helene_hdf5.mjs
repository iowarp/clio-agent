// The Helene HDF5 demo as a person would work it: one plain request per turn, no tool named.
//
//   WS=<workspace> SID=<session> node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_hdf5.mjs --label helene --width 1600 --height 1000
//
// The workspace holds era5/msl_2024-09.nc and era5/sst_2024-09.nc and has the ERA5
// pressure-level bucket linked (bench/live/setup_ws.py). The person asks what is in the
// pressure file (the file @-referenced from the composer), opens the field from the answer,
// reads its attributes and looks at it, adds /MSL from the viewer and asks to track the
// storm; then asks how it deepened with /SSTK @-referenced from the composer (Tab into the
// file); then the clouds in 3D; then a dashboard to share. No turn names a tool.
import { api, send, waitIdle } from "./_lib.mjs";

const MSL = "msl_2024-09.nc";
const SST = "sst_2024-09.nc";
const TURNS = {
  contents: "What's in here?",
  track:
    "Hurricane Helene crossed the Gulf in late September 2024. Can you track its centre and " +
    "central pressure in this from the 24th to the 27th?",
  deepen: "How fast did it deepen, and was the water under it warm enough to explain that?",
  // The person names the viewer they want: without it the agent sometimes builds its own
  // mesh view instead, a valid choice that the rest of this script does not follow.
  clouds:
    "What did the clouds around it look like on the 26th, before it came ashore? The linked " +
    "ERA5 bucket has cloud water; those files are big. Show me in 3D in ParaView.",
  share: "Put all of this on a dashboard so I can share it with colleagues.",
  follow: "Can I step through the days on this and have everything follow, the 3D clouds too?",
  go: "3-hourly, go ahead.",
  beat:
    "I stepped the 3D view to landfall and raised the liquid-water surface. What does it " +
    "show now, and how does it compare with the day before?",
};

const T0 = Date.now();
/** A timestamp from the start of the recording, for cutting the video into beats. */
function mark(name) {
  console.log(`MARK ${name} ${((Date.now() - T0) / 1000).toFixed(1)}`);
}

async function settle(page, sid, log, shot, name, minutes = 25) {
  mark(`${name}-waiting`);
  let n = 0;
  const until = Date.now() + minutes * 60_000;
  while (Date.now() < until) {
    const done = await waitIdle(page, sid, log, 4);
    await shot(`${name}-${String(++n).padStart(2, "0")}`);
    if (done) {
      await toLatest(page);
      mark(`${name}-answered`);
      return true;
    }
  }
  mark(`${name}-timeout`);
  return false;
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
    await page.waitForTimeout(6000);
    const viewer = page.frameLocator('iframe[title^="HDF5 viewer"]');
    await viewer.getByRole("button", { name: "Explorer" }).click()
      .catch(() => page.getByRole("button", { name: "Explorer" }).click());
    await viewer.getByText("MSL", { exact: true }).first().click();
  }
  await page.waitForTimeout(8000);
  await shot("field-open");
  // Reads what the field is, then looks at it.
  await page.getByRole("button", { name: "Attributes" }).click().catch(() => {});
  await page.waitForTimeout(4000);
  await shot("field-attributes");
  await page.getByRole("button", { name: "Image" }).click().catch(() => {});
  await page.waitForTimeout(6000);
  const viewer = page.frameLocator('iframe[title^="HDF5 viewer"]');
  // ERA5 stores latitude north to south, so the raw array draws south up: flip it.
  await viewer.getByRole("button", { name: "More controls" }).click().catch(() => {});
  await viewer.getByRole("button", { name: "Flip Y" }).click().catch(() => {});
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(1500);
  // Scrubs time to the storm in the Gulf (26 Sep 12Z is step 612 of 720).
  const slider = viewer.getByRole("slider").last();
  const box = await slider.boundingBox().catch(() => null);
  if (box) {
    await page.mouse.move(box.x + 2, box.y + box.height / 2);
    await page.mouse.down();
    // Measured: the track is not the box; 0.75 of it is step ~612 (26 Sep 12Z).
    await page.mouse.move(box.x + 2 + (box.width - 2) * 0.7516, box.y + box.height / 2, { steps: 25 });
    await page.mouse.up();
    await page.waitForTimeout(4000);
  }
  await shot("field-image");
}

/** The person's own turn in the dashboard: step the 3D view, raise a surface, then ask. */
async function finalBeat(page, shot, sid, log) {
  mark("beat-dashboard");
  // The dashboard may already be open (the agent shows what it published); otherwise the
  // newest dashboard card in the conversation opens it in the canvas.
  const live = page.locator('iframe[title*="ParaView"]');
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
  const pv = page.locator('iframe[title*="ParaView"]').first();
  await pv.scrollIntoViewIfNeeded();
  await page.waitForTimeout(12000);
  const app = page.frames().find((f) => f.url().endsWith("/index.html"));
  if (!app) throw new Error("the live 3D view did not load in the dashboard");
  // Rendered on the ParaView machine, the view keeps drawing when the canvas grows.
  await app.getByRole("button", { name: "Render on ParaView machine" }).click();
  await page.waitForTimeout(5000);
  await shot("beat-dashboard");
  // Step the 3D view to landfall (27 Sep 03Z is step 26 of 32); the other views follow.
  const time = app.getByRole("slider", { name: /Time step/ }).first();
  await time.focus();
  // The counter updates after the server moves the view, so step and wait for it.
  for (let k = 0; k < 40; k += 1) {
    const now = Number((await stepOf(app)).split("/")[0]);
    if (now === 26) break;
    await time.focus();
    await page.keyboard.press(now < 26 ? "ArrowRight" : "ArrowLeft");
    await page.waitForTimeout(1200);
  }
  await page.waitForTimeout(6000);
  await shot("beat-landfall");
  // Raise the liquid-water surface with its slider.
  // The agent names its contours; pick the liquid one whatever it called it. The panel
  // remembers an open drawer, so open the pipeline only when its list is not showing.
  const liquid = app.getByRole("button", { name: /^(?!Hide |Show |Remove ).*liquid/i }).first();
  if (!(await liquid.isVisible().catch(() => false))) {
    await app.getByRole("button", { name: "Pipeline" }).click();
  }
  await liquid.click();
  // Add filter is a toggle too: open it only when its Edit button is not showing.
  const edit = app.getByRole("button", { name: /^Edit/ }).first();
  if (!(await edit.isVisible().catch(() => false))) {
    await app.getByRole("button", { name: "Add filter" }).click();
  }
  await edit.click();
  // Types the raised liquid surface (the slider does not yet track drags; see the
  // ParaView panel task) and applies it.
  // Raises the liquid surface by dragging its first value's slider to the right
  // (a log slider when the field spans decades), then lets go: release applies it.
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
  await page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).click().catch(() => {});
  if (process.env.BEAT_ASK === "0") return;
  await send(page, TURNS.beat);
  await settle(page, sid, log, shot, "beat", 15);
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
  return (await app.getByText(/^\s*\d+\s*\/\s*32\s*$/).first().textContent().catch(() => "0/32")).trim();
}

export default async function (page, shot) {
  const ws = process.env.WS;
  const sid = process.env.SID;
  const only = (process.env.TURNS || Object.keys(TURNS).join(",")).split(",");
  const log = [];
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  const composer = page.locator('textarea, [contenteditable="true"]').last();
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await page.waitForTimeout(2000);
  // Only this conversation on screen: the workspace list is hidden for the recording.
  // The app remembers the sidebar's state, so hide it only when it is showing.
  const listShown = await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false);
  if (process.env.HIDE_SIDEBAR !== "0" && listShown) {
    await page.getByRole("button", { name: "Toggle Sidebar" }).first().click().catch(() => {});
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
  }
  if (only.includes("beat")) await finalBeat(page, shot, sid, log);
  await shot("final");
  console.log("LOG", JSON.stringify(log));
}
