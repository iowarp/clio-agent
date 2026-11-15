// Reopen conversations after a restart or update (the continuity grid,
// scripts/acceptance/continuity_check.py) and capture each one as a person sees it.
//
//   CC_WORKSPACE=ws_default CC_SESSIONS=sess_a,sess_b node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/continuity_open.mjs --label cc-A
//
// For each conversation it prints the title, the reply-failure lines it shows ("This reply
// didn't load", "This reply didn't finish", "This reply was interrupted") with the reason
// line under each, and whether the composer accepts input. With CC_ASK set it also sends
// that text in the last conversation and captures the answer.
export default async function (page, shot) {
  const ws = process.env.CC_WORKSPACE || "ws_default";
  const sessions = (process.env.CC_SESSIONS || "").split(",").filter(Boolean);
  const report = [];
  for (const [i, sid] of sessions.entries()) {
    const url = new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString();
    // Navigate inside the app after the first load: a full reload per conversation makes
    // the dev server's module graph exhaust the headless browser.
    if (i === 0) await page.goto(url);
    else
      await page.evaluate((target) => {
        window.history.pushState({}, "", target);
        window.dispatchEvent(new PopStateEvent("popstate"));
      }, url);
    await page.waitForTimeout(6000);
    const body = await page.locator("body").innerText();
    const lines = body.split("\n").map((l) => l.trim()).filter(Boolean);
    const failures = [];
    lines.forEach((line, i) => {
      if (/^This reply (didn.t load|didn.t finish|was interrupted)/.test(line))
        failures.push(`${line} | ${lines[i + 1] || ""}`);
    });
    const composer = page.getByRole("textbox").last();
    report.push({
      sid,
      failures,
      composerEnabled: (await composer.count()) ? await composer.isEditable() : false,
      notFound: /not found|couldn.t (load|open)/i.test(body),
    });
    // The newest turns are what a reopened conversation shows first.
    await shot(sid);
  }
  if (process.env.CC_ASK && sessions.length) {
    const composer = page.getByRole("textbox").last();
    await composer.fill(process.env.CC_ASK);
    await composer.press("Enter");
    await page.waitForTimeout(Number(process.env.CC_ASK_WAIT || 25000));
    await shot("answer");
  }
  console.log(JSON.stringify({ continuity: report }, null, 2));
}
