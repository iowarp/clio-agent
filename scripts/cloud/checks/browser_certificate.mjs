// W21 check: a plain request (CHECK_ASK) for a page of a device with a self-signed
// certificate, answered by browser_shared_tab.mjs; then, as the person, open the page from
// the Work rail, capture Chromium's warning with the Trust control, trust the certificate
// for this conversation, capture the page after the browser restarts, and send
// CHECK_FOLLOWUP so the agent reads the page.
import shared from "./browser_shared_tab.mjs";

export default async function (page, shot) {
  if (process.env.CHECK_ASK) await shared(page, shot);
  else if (process.env.CHECK_SESSION_PATH) {
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  const host = process.env.CHECK_CERT_HOST || "c.fixture.localhost";
  const row = page.getByRole("button", { name: /^Open / }).filter({
    has: page.getByText(host, { exact: true }),
  });
  const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
  for (let i = 0; i < 4 && !(await row.first().isVisible().catch(() => false)); i += 1) {
    await layout.click();
    await page.waitForTimeout(1000);
  }
  await row.first().click();
  for (let i = 0; i < 4; i += 1) {
    // Fold the evidence rail away so it does not cover the top of the Browser tab.
    const name = (await layout.getAttribute("aria-label").catch(() => "")) || "";
    if (/: None$/u.test(name)) break;
    await layout.click();
    await page.waitForTimeout(800);
  }
  const notice = page.locator('[data-slot="browser-certificate-notice"]').first();
  await notice.waitFor({ timeout: 20000 });
  await page.waitForTimeout(2000);
  await shot("certificate-warning");
  const details = await notice
    .getByRole("button", { name: /^Certificate details/ })
    .first()
    .getAttribute("aria-label");
  await notice.getByRole("button", { name: "This conversation" }).click();
  await page.waitForTimeout(9000);
  await shot("trusted");
  const address = await page.getByRole("textbox", { name: "Address" }).inputValue().catch(() => "");
  const stillWarning = await notice.isVisible().catch(() => false);
  console.error(JSON.stringify({ details, addressAfterTrust: address, stillWarning }));
  if (process.env.CHECK_FOLLOWUP) {
    process.env.CHECK_ASK = process.env.CHECK_FOLLOWUP;
    delete process.env.CHECK_SESSION_PATH;
    await shared(page, shot);
  }
}
