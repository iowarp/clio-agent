// Looking at and commenting on a client-rendered file view (ledger "Looking at and
// commenting on client-rendered files (FV)"). The 3D view of a STEP whistle: a section
// through the air channel and a wall measurement on screen, then plain requests only.
//
//   FV_SESSION  "<workspace id>/<session id>": the conversation whose workspace has the file
//   FV_FILE     the file to open from Files (name fragment, default .step)
//   FV_PHASE    comment (default): mark the wall and send "Is this wall thick enough to print?"
//               look: select a part, then ask the agent to look at the model
//               phone: the comment flow at phone width (no hero layout); phone-look: the look
//   FV_COMMENT  the comment typed on the region (default the wall question)
//   FV_LOOK     the look request
//   FV_OUT      a file that receives the turns' tool calls and answers
//   FV_STOP     "measure": stop after the section and the wall measurement (calibration)
// Waits for each turn, allowing the session's permission asks as a person would.
import { writeFileSync } from "node:fs";
import { allowPending, api, send } from "./_lib.mjs";
import {
  canvasBox,
  cutFacing,
  gizmo,
  hero,
  measureWall,
  moreRow,
  openFile,
  panelText,
  result as cad,
  toolbar,
  view,
  visible,
} from "./cad_tools.mjs";

const env = process.env;
const phase = env.FV_PHASE || "comment";
const phone = phase.startsWith("phone");
const out = { phase, log: [], turns: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[fv] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

function sessionOf(page) {
  return decodeURIComponent(new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/)[1]);
}

async function settle(page, minutes = 15) {
  const sid = sessionOf(page);
  const until = Date.now() + minutes * 60_000;
  const allowed = [];
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sid, allowed);
    const session = await api("GET", `/v1/sessions/${sid}`);
    quiet = ["idle", "completed", "error"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) break;
    await page.waitForTimeout(3000);
  }
  if (allowed.length) note("allowed", allowed);
}

async function lastTurn(page) {
  const listed = await api("GET", `/v1/sessions/${sessionOf(page)}/messages`);
  const rows = [...(Array.isArray(listed) ? listed : listed.messages || listed.items || [])].reverse();
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  const turn = rows.slice(lastUser + 1);
  const calls = turn.flatMap((m) =>
    (m.parts || []).filter((p) => p.type === "tool_call").map((p) => p.tool_name),
  );
  const answer = turn
    .flatMap((m) => (m.parts || []).filter((p) => p.type === "text").map((p) => p.text))
    .join("\n")
    .trim();
  return { calls, answer };
}

async function turn(page, shot, text, label) {
  await send(page, text);
  await page.waitForTimeout(4000);
  await settle(page);
  const found = await lastTurn(page);
  out.turns.push({ label, text, ...found });
  note(`${label} calls`, found.calls);
  note(`${label} answer`, found.answer.slice(0, 2000));
  await page.waitForTimeout(2000);
  await shot(label);
}

async function restore(page) {
  const button = page.getByRole("button", { name: /Restore canvas|Exit maximized|Minimize canvas/ }).first();
  if (await button.isVisible().catch(() => false)) await button.click();
  await page.waitForTimeout(3000);
}

/** The air channel cut open, seen face on, with the floor wall measured across the cut. */
async function sectionAndWall(page, shot) {
  await toolbar(page, "Section");
  await page.waitForTimeout(800);
  await gizmo(page, await cutFacing(page));
  await toolbar(page, "Measure");
  await page.waitForTimeout(500);
  await moreRow(page, "Orthographic");
  if (phone) {
    // A phone shows the whole whistle a few pixels tall: zoom in on its middle first.
    const box = await canvasBox(page);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    for (let i = 0; i < 8; i += 1) {
      await page.mouse.wheel(0, -240);
      await page.waitForTimeout(120);
    }
    await page.waitForTimeout(600);
  }
  if (phone) {
    // Touch has no hover to scan with: tap the floor wall near its outer and inner lines
    // (a tap on the outer line, and one on the far wall just above the floor, each snapping to its nearer line), which this zoom puts at these fractions of the
    // canvas with the Measure and Section sheets open (read from fv-cal-3-phone-measure-ready).
    const box = await canvasBox(page);
    await shot("phone-measure-ready");
    for (const gy of [0.434, 0.404])
      await page.touchscreen.tap(box.x + box.width * 0.67, box.y + box.height * gy);
    await page.waitForTimeout(600);
    out.wallFound = true;
  } else {
    // Columns across the resonator, where the floor is one wall between chamber and outside.
    out.wallFound = await measureWall(page, [0.6, 0.65, 0.55, 0.7, 0.5]);
  }
  await moreRow(page, "Orthographic");
  await page.mouse.move(5, 5);
  out.section = await panelText(page, "cad-section");
  out.wall = await panelText(page, "cad-measure");
  note("section", out.section);
  note("wall", out.wall);
  // The tool closes; its measurement stays on screen with its label.
  await toolbar(page, "Measure");
  await page.waitForTimeout(600);
  await shot("section-wall");
}

async function comment(page, shot) {
  const frame = visible(page.locator('[data-slot="file-viewer"]'));
  await frame.getByRole("button", { name: "Comments" }).first().click();
  await page.waitForTimeout(1500);
  const layer = visible(view(page).locator('[data-slot="comment-region-layer"]'));
  const box = await layer.boundingBox();
  if (!box) throw new Error("the comment layer did not open over the 3D view");
  // A region around the wall's measurement label, else the middle of the view.
  const label = visible(page.locator('[data-slot="cad-measure-labels"] > div'));
  const at = (await label.count()) ? await label.boundingBox() : null;
  const cx = at ? at.x + at.width / 2 : box.x + box.width / 2;
  const cy = at ? at.y + at.height / 2 : box.y + box.height / 2;
  const half = phone ? 70 : 110;
  const from = [Math.max(box.x + 2, cx - half * 1.4), Math.max(box.y + 2, cy - half)];
  const to = [Math.min(box.x + box.width - 2, cx + half * 1.4), Math.min(box.y + box.height - 2, cy + half)];
  await page.mouse.move(from[0], from[1]);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(from[0] + ((to[0] - from[0]) * i) / 12, from[1] + ((to[1] - from[1]) * i) / 12);
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
  const editor = page.getByPlaceholder("What should the agent notice here?");
  await editor.waitFor({ timeout: 20000 });
  await editor.fill(env.FV_COMMENT || "Is this wall thick enough to print?");
  await page.waitForTimeout(4000); // the mark's snapshot
  await shot("comment-typed");
  await page.getByRole("button", { name: /^Done$/ }).click();
  await page.waitForTimeout(1500);
  await shot("comment-marked");
  await page.getByRole("button", { name: /Add 1 mark to message/ }).click();
  await page.waitForTimeout(1500);
  if (phone) {
    const close = page.getByRole("button", { name: "Close workspace canvas" }).filter({ visible: true });
    // The sheet covers the button, so the click goes to the button itself.
    if (await close.count()) await close.first().evaluate((button) => button.click());
    else await page.keyboard.press("Control+Shift+B"); // the canvas toggle's shortcut
    await page.waitForTimeout(2000);
  } else await restore(page);
  await shot("comment-attached");
  await turn(page, shot, "", "comment");
}

export default async function (page, shot) {
  const [workspace, session] = (env.FV_SESSION || "").split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  await openFile(page, env.FV_FILE || ".step");
  if (!phone) await hero(page);
  await shot("open");
  if (phase === "look" || phase === "phone-look") {
    // Select the plug from the parts list; the look request names no tool.
    // A phone starts with the parts list folded (More > Parts list).
    if (!(await page.locator('[data-slot="cad-parts"]').filter({ visible: true }).count()))
      await moreRow(page, "Parts list");
    const plug = page.locator('[data-slot="cad-parts"] button[title^="Select"]').last();
    if (await plug.count()) await plug.click();
    await page.waitForTimeout(800);
    out.selected = await panelText(page, "cad-selection");
    note("selected", out.selected);
    await shot("selected");
    if (phone) {
      const close = page.getByRole("button", { name: "Close workspace canvas" });
      if (await close.count()) await close.first().evaluate((button) => button.click());
      await page.waitForTimeout(2000);
    } else await restore(page);
    await turn(
      page,
      shot,
      env.FV_LOOK || "Look at the model I have open: how big is it, and which part is selected?",
      "look",
    );
    if (!phone) {
      await hero(page);
      await shot("look-hero");
    }
  } else {
    await sectionAndWall(page, shot);
    if (env.FV_STOP === "measure") return;
    out.canvas = await canvasBox(page);
    await comment(page, shot);
    if (!phone) {
      await hero(page);
      await shot("comment-answer-hero");
    }
  }
  out.cad = cad;
  if (env.FV_OUT) writeFileSync(env.FV_OUT, JSON.stringify(out, null, 2));
}
