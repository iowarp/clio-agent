// MA19 / AQ25: open a conversation whose app view CLIO no longer holds (closed, or CLIO
// restarted), reopen it from its card and check the view mounts again. REOPEN_WORKSPACE and
// REOPEN_SESSION name the conversation. REOPEN_ALLOW=1 answers the replay's permission card
// with "Allow once" (the replay follows the session's permission rules). REOPEN_EXPECT=fail
// expects the replay to fail: it captures the reason and the saved call, then (with
// REOPEN_RETRY=1) clicks Retry after REOPEN_RETRY_DELAY ms. Prints what the card shows.
export default async function (page, shot) {
  const ws = process.env.REOPEN_WORKSPACE || "ws_default";
  const sid = process.env.REOPEN_SESSION;
  await page.goto(new URL(`/workspaces/${ws}/sessions/${sid}`, page.url()).toString());
  const reopen = page.locator("[data-mcp-app-reopen]:visible").last().getByRole("button", { name: "Reopen" });
  await reopen.waitFor({ timeout: 60000 });
  await reopen.scrollIntoViewIfNeeded();
  await shot("closed-card");
  await reopen.click();
  const allowOnce = page.getByRole("button", { name: /Allow once/ });
  if (process.env.REOPEN_ALLOW === "1") {
    await allowOnce.waitFor({ timeout: 60000 });
    await shot("replay-asks");
    await allowOnce.click();
  }
  const report = {};
  if (process.env.REOPEN_EXPECT === "fail") {
    const alert = page.locator("[data-reopen-reason]").last();
    await alert.waitFor({ timeout: 60000 });
    await alert.scrollIntoViewIfNeeded();
    report.reason = await alert.getAttribute("data-reopen-reason");
    report.failure = await alert.innerText();
    await shot("replay-failed");
    if (process.env.REOPEN_RETRY === "1") {
      await page.waitForTimeout(Number(process.env.REOPEN_RETRY_DELAY || 0));
      await alert.getByRole("button", { name: "Retry" }).click();
      if (process.env.REOPEN_ALLOW === "1") {
        await allowOnce.waitFor({ timeout: 60000 });
        await allowOnce.click();
      }
    } else {
      console.log(JSON.stringify(report, null, 2));
      return;
    }
  }
  const frame = page.locator("iframe[data-mcp-app-iframe]").last();
  await frame.waitFor({ timeout: 60000 });
  report.appInstanceId = await frame.getAttribute("data-mcp-app-iframe");
  const view = page.frameLocator("iframe[data-mcp-app-iframe]").last().frameLocator("iframe");
  const call = view.locator("#call");
  await call.filter({ hasText: "Produced by call" }).waitFor({ timeout: 60000 });
  report.view = {
    title: await view.locator("#title").innerText(),
    args: await view.locator("#args").innerText(),
    call: await call.innerText(),
  };
  await frame.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  await shot("reopened");
  console.log(JSON.stringify(report, null, 2));
}
