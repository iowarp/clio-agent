// Browser check: send a plain request (CHECK_ASK) and answer each permission question the
// way a person would, then capture the conversation and the browser calls nested under
// their run_code rows. Environment:
//   CHECK_ASK            the request (no tool or step named)
//   CHECK_DURATION       duration for allowed answers (default "This session")
//   CHECK_DENY_HOST      a host whose site question is denied instead
//   CHECK_HOST_DELAY_MS  wait this long before answering a site question (W2: 45 s)
//   CHECK_SETTLE_MS      bound on the turn (default 7 minutes)
// It prints {"answered": [...], "answer": "<final reply>"} on stderr.
const ALLOW =
  /^(Allow once|Allow for session|Allow for workspace|Allow for the rest of this script)$/;

export default async function (page, shot) {
  const ask = process.env.CHECK_ASK;
  const duration = process.env.CHECK_DURATION || "This session";
  const denyHost = process.env.CHECK_DENY_HOST || "";
  const hostDelay = Number(process.env.CHECK_HOST_DELAY_MS || 0);
  const settle = Number(process.env.CHECK_SETTLE_MS || 420000);
  if (process.env.CHECK_SESSION_PATH) {
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  const doneBefore = await page.getByText("Done", { exact: true }).count();
  if (ask) {
    const composer = page.locator('textarea, [contenteditable="true"]').first();
    await composer.click();
    await page.keyboard.type(ask);
    await page.keyboard.press("Enter");
  }
  const deadline = Date.now() + settle;
  await page.waitForTimeout(3000);
  const answered = [];
  while (Date.now() < deadline) {
    const done = page.getByText("Done", { exact: true });
    if ((await done.count()) > doneBefore && (await done.last().isVisible().catch(() => false))) break;
    const allow = page.getByRole("button", { name: ALLOW });
    if (await allow.count()) {
      const card = page.locator('[data-slot="pending-interaction"], [role="region"]').filter({ has: allow.first() }).last();
      const text = (await card.innerText().catch(() => "")) || (await page.locator("body").innerText()).slice(-3000);
      const site = /Site\s+(\S+)/u.exec(text)?.[1] ?? "";
      const title = text.split("\n").find((line) => line.trim() && !/attention/i.test(line))?.trim() ?? "";
      await allow.first().scrollIntoViewIfNeeded().catch(() => {});
      await shot(`question-${answered.length + 1}`);
      if (site && hostDelay) await page.waitForTimeout(hostDelay);
      if (site && denyHost && site === denyHost) {
        await page.getByRole("button", { name: /^Deny$/ }).first().click();
        answered.push({ title, site, answer: "deny" });
      } else {
        const picker = page.getByRole("combobox", { name: "Permission duration" }).first();
        if (await picker.count()) {
          await picker.click();
          await page.getByRole("option", { name: duration }).click().catch(() => {});
        }
        await page.getByRole("button", { name: ALLOW }).first().click();
        answered.push({ title, site, answer: site ? duration : "allowed" });
      }
      await page.waitForTimeout(1500);
      continue;
    }
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(1500);
  await shot("answered");
  const answer = await page
    .locator('[data-slot="message-response"], [data-role="assistant"]')
    .last()
    .innerText()
    .catch(() => "");
  const groups = page.getByRole("button", { name: /^Activity: / });
  for (let i = 0; i < (await groups.count()); i += 1) {
    const group = groups.nth(i);
    if ((await group.getAttribute("aria-expanded")) === "false") {
      await group.click().catch(() => {});
      await page.waitForTimeout(300);
    }
  }
  console.error(JSON.stringify({ answered, url: page.url(), answer: answer.slice(0, 1500) }));
}
