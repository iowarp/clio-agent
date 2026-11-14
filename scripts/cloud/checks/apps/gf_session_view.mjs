// Final gates (GF): open a conversation that already holds a view and shoot it, at any width.
// A smoke step for the scenarios a plain request already ran (desktop or phone): it reads
// the conversation's last answer and its views, without sending anything.
//
//   GF_SESSION  "<workspace id>/<session id>"
//   GF_TAB=1    also open the latest view's card in its tab ("Open in tab") and shoot it; at
//               desktop width the shot is a hero (navigation collapsed, canvas maximized)
//   GF_OPEN     the visible text of a card to open (a dashboard, a file), then shoot it
//   GF_WAIT     seconds to wait for the views to draw after the page loads (default 20)
//
// Prints one JSON line: the app frames, the file views and the last answer's first words.
import { appFrames, heroShot, showLastApp } from "./_lib.mjs";

export default async function (page, shot) {
  const env = process.env;
  const [workspace, session] = env.GF_SESSION.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(Number(env.GF_WAIT || 20) * 1000);
  const frames = await showLastApp(page, await appFrames(page));
  await shot("conversation");
  const answer = await page
    .locator('[data-message-role="assistant"], [data-role="assistant"]')
    .last()
    .innerText()
    .catch(() => "");
  let tab = null;
  if (env.GF_OPEN) {
    const card = page.getByText(env.GF_OPEN, { exact: true }).last();
    await card.scrollIntoViewIfNeeded().catch(() => undefined);
    await card.click();
    await page.waitForTimeout(12000);
    await shot("opened");
    await page.mouse.wheel(0, 500);
    await page.waitForTimeout(2000);
    await shot("opened-scrolled");
  }
  if (env.GF_TAB === "1") {
    const open = page.getByRole("button", { name: /Open in tab/i }).last();
    if (await open.count()) {
      await open.scrollIntoViewIfNeeded().catch(() => undefined);
      await open.click();
      await page.waitForTimeout(8000);
      tab = await page.locator("[data-mcp-app-tab]:visible, [data-file-view]:visible").count();
      await shot("tab");
      if ((page.viewportSize()?.width ?? 0) >= 1000) await heroShot(page, shot, "hero");
    }
  }
  console.log(
    "GF_VIEW " +
      JSON.stringify({
        session,
        frames,
        tab,
        answer: answer.replace(/\s+/g, " ").slice(0, 400),
      }),
  );
}
