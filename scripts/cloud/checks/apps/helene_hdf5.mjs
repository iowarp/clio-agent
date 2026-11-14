// The Helene HDF5 demo as a person would work it: one plain request per turn, no tool named.
//
//   WS=<workspace> SID=<session> node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_hdf5.mjs --label helene --width 1600 --height 1000
//
// The workspace holds era5/msl_2024-09.nc and has the ERA5 pressure-level bucket linked
// (bench/live/setup_ws.py). Turns: what is in the file (@ from the composer), a look in
// Files, track the storm (/MSL chosen in the viewer and added), how it deepened, the clouds
// in 3D, then a dashboard to share. A run with a turn naming tools would prove the
// mechanism only; these do not.
import { send, waitIdle } from "./_lib.mjs";

const FILE = "msl_2024-09.nc";
const TURNS = {
  contents: "What's in this file?",
  track:
    "Hurricane Helene crossed the Gulf in late September 2024. Can you track its centre and " +
    "central pressure in this from the 24th to the 27th?",
  deepen: "How fast did it deepen, and does ERA5 get its strength right?",
  clouds:
    "What did the clouds around it look like on the 26th, as it came ashore? The linked ERA5 " +
    "bucket has cloud water; those files are big. Show me in 3D.",
  share: "Put all of this on a dashboard so I can share it with colleagues.",
};

async function settle(page, sid, log, shot, name, minutes = 25) {
  let n = 0;
  const until = Date.now() + minutes * 60_000;
  while (Date.now() < until) {
    const done = await waitIdle(page, sid, log, 4);
    await shot(`${name}-${String(++n).padStart(2, "0")}`);
    if (done) return true;
  }
  return false;
}

async function openFile(page) {
  if (await page.getByText(FILE).count()) return;
  await page.getByRole("button", { name: "Open workspace canvas" }).click().catch(() => {});
  await page.getByRole("tab", { name: "Files" }).first().click().catch(() => {});
  await page.getByText("era5", { exact: true }).first().click().catch(() => {});
  await page.getByText(FILE).first().click();
  await page.waitForTimeout(10000);
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

  if (only.includes("contents")) {
    // The whole file, referenced from the composer's @ menu.
    await composer.click();
    await page.keyboard.type(`@${FILE.slice(0, 3)}`, { delay: 80 });
    await page.getByRole("option", { name: new RegExp(`^${FILE}era5`) }).waitFor();
    await page.keyboard.press("Enter");
    await page.keyboard.type(" " + TURNS.contents);
    await shot("contents-request");
    await page.keyboard.press("Enter");
    await settle(page, sid, log, shot, "contents");
  }
  if (only.includes("track")) {
    // A look at the field in Files, then the dataset chosen there and added to the message.
    await openFile(page);
    await shot("files-field");
    const viewer = page.frameLocator('iframe[title^="HDF5 viewer"]');
    await viewer.getByRole("button", { name: "Explorer" }).click()
      .catch(() => page.getByRole("button", { name: "Explorer" }).click());
    await viewer.getByText("MSL", { exact: true }).first().click();
    await page.getByRole("button", { name: "Add to message" }).click();
    const chip = page.locator('[data-slot="composer-toolbar"]').locator("xpath=..");
    if (!(await chip.innerText()).includes("/MSL")) throw new Error("the /MSL reference was not added");
    await composer.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" " + TURNS.track);
    await shot("track-request");
    await page.keyboard.press("Enter");
    await settle(page, sid, log, shot, "track");
  }
  for (const name of ["deepen", "clouds", "share"]) {
    if (!only.includes(name)) continue;
    await send(page, TURNS[name]);
    await settle(page, sid, log, shot, name, name === "deepen" ? 15 : 35);
  }
  await shot("final");
  console.log("LOG", JSON.stringify(log));
}
