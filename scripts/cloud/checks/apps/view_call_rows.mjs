// Live check: a view's own call made while a turn streamed is its own message (fix stream).
//
// CHECK_SESSION_PATH=/workspaces/<ws>/sessions/<sid> names a conversation in which
// scripts/cloud/checks/apps/external_call_hang.sh (or a surface) made a view call while
// a turn was answering. The step opens it, scrolls to the end and captures the last
// answer and the view call's own message ("mcp_app" origin): the answer's text is one
// block and the call's row sits in a message of its own.
export default async function (page, shot) {
  const target = process.env.CHECK_SESSION_PATH;
  if (!target) throw new Error("CHECK_SESSION_PATH is required");
  await page.goto(new URL(target, page.url()).toString());
  await page.waitForTimeout(5000);
  await page.keyboard.press("End").catch(() => {});
  await page.mouse.wheel(0, 20000);
  await page.waitForTimeout(1500);
  await shot("view-call-own-message");
  const text = await page.locator("main").innerText().catch(() => "");
  const answerStart = text.indexOf("Engineers rarely read a time series");
  return {
    answer_visible: answerStart >= 0,
    called_rows: (text.match(/Called \d+ tool/g) || []).length,
  };
}
