// offer_plugin's search rows in a real conversation (ledger OLS4): OL_PATH is the
// conversation's UI path. Opens the tool group, expands every "Search plugins" /
// "Plugins in" row and shoots it. Prints OFFER_ROWS [{row, text}].
export default async function (page, shot) {
  await page.goto(new URL(process.env.OL_PATH, page.url()).toString(), { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(8000);
  // Grouped calls ("Called 2 tools") open first.
  for (const group of await page.getByText(/^Called \d+ tools?/).all())
    await group.click().catch(() => undefined);
  await page.waitForTimeout(1500);
  const openers = page.getByRole("button", { name: /^(Expand|Show result for) (Search plugins|Plugins in)/ });
  const out = [];
  for (const opener of await openers.all()) {
    await opener.click().catch(() => undefined);
    await page.waitForTimeout(800);
    const row = opener.locator('xpath=ancestor::*[starts-with(@id, "tool-")][1]');
    out.push({
      row: await row.getAttribute("id").catch(() => null),
      text: (await row.innerText().catch(() => "")).replace(/\s+/g, " ").slice(0, 400),
    });
  }
  if (out.length) await openers.first().scrollIntoViewIfNeeded();
  await page.waitForTimeout(1000);
  await shot("search-rows");
  console.log(`OFFER_ROWS ${JSON.stringify(out)}`);
}
