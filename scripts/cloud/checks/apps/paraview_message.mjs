// A plain request to the ParaView connector whose answer is text (AQ35, AQ43).
//
// PV_PROMPT is the request. The step starts a new conversation, allows the
// connector's tools for it when CLIO asks, waits for the turn to end, and prints
// the session id and the agent's last message.
const prompt = process.env.PV_PROMPT || "Open /data/wavelet.vti in ParaView.";

export default async function (page, shot) {
  await page.getByText("Loading workspace").waitFor({ state: "hidden", timeout: 180000 });
  const fresh = page.getByText("New conversation", { exact: true }).filter({ visible: true }).first();
  await fresh.waitFor({ state: "visible", timeout: 180000 });
  await fresh.click();
  await page.waitForTimeout(1500);
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.click();
  await page.keyboard.type(prompt);
  await page.keyboard.press("Enter");
  let quiet = 0;
  let started = false;
  const deadline = Date.now() + 600000;
  while (Date.now() < deadline && quiet < 6) {
    const allow = page.getByRole("button", { name: /This session/ });
    if (await allow.count()) await allow.first().click().catch(() => undefined);
    const stop = page.getByRole("button", { name: /^Stop/ });
    const busy = (await stop.count()) > 0 && (await stop.first().isVisible().catch(() => false));
    if (busy) started = true;
    quiet = started && !busy ? quiet + 1 : 0;
    await page.waitForTimeout(1000);
  }
  await shot("answer");
  const sid = (page.url().match(/sess_[A-Za-z0-9_-]+/) || [null])[0];
  const messages = page.locator('[data-message-role="assistant"], [data-role="assistant"]');
  const answer = (await messages.count()) ? await messages.last().innerText() : null;
  console.log(JSON.stringify({ prompt, session: sid, answer }, null, 2));
}
