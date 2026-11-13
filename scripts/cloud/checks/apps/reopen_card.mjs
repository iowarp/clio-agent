// MA19 at any width: open a conversation (REOPEN_WORKSPACE, REOPEN_SESSION), expand its
// activity, and reopen the last visible closed App card. With REOPEN_EXPECT=fail the replay
// is expected to fail (the fixture's fail file is present): the step captures the reason and
// the saved call, removes REOPEN_FAIL_FILE when given, and clicks Retry. Each permission
// ask is answered with "Allow once". Ends with the reopened view on screen.
import { existsSync, rmSync } from "node:fs";

async function allowOnce(page) {
  const button = page.getByRole("button", { name: /Allow once/ });
  if (await button.first().waitFor({ timeout: 20000 }).then(() => true, () => false))
    await button.first().click();
}

export default async function (page, shot) {
  const ws = process.env.REOPEN_WORKSPACE || "ws_default";
  const sid = process.env.REOPEN_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  await page.waitForTimeout(6000);
  for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
    await group.click().catch(() => undefined);
  const line = page.locator("[data-mcp-app-reopen]:visible").last();
  await line.waitFor({ timeout: 60000 });
  await line.scrollIntoViewIfNeeded();
  await shot("closed-card");
  await line.getByRole("button", { name: "Reopen" }).click();
  await allowOnce(page);
  const report = {};
  if (process.env.REOPEN_EXPECT === "fail") {
    const alert = page.locator("[data-reopen-reason]:visible").last();
    await alert.waitFor({ timeout: 90000 });
    await alert.scrollIntoViewIfNeeded();
    report.reason = await alert.getAttribute("data-reopen-reason");
    report.failure = await alert.innerText();
    await shot("replay-failed");
    const fail = process.env.REOPEN_FAIL_FILE;
    if (fail && existsSync(fail)) rmSync(fail);
    await alert.getByRole("button", { name: "Retry" }).click();
    await allowOnce(page);
  }
  const frame = page.locator("[data-mcp-app-iframe]:visible").last();
  await frame.waitFor({ timeout: 90000 });
  const view = page.frameLocator("[data-mcp-app-iframe]:visible").last().frameLocator("iframe");
  await view.locator("#call").filter({ hasText: "Produced by call" }).waitFor({ timeout: 90000 });
  report.view = await view.locator("main").innerText();
  await frame.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  await shot("reopened");
  console.log(JSON.stringify(report, null, 2));
}
