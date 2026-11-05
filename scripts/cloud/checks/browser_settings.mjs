// Check: Settings > Browser shows the browser choices, and saving one applies it.
// CHECK_CHANNEL_LABEL names the choice to pick for "Separate browser"
// (default "Installed Google Chrome").
export default async function (page, shot) {
  await page.goto(new URL("/settings/browser", page.url()).toString());
  await page.getByText("Separate browser").first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(1000);
  await shot("browser-settings");
  const label = process.env.CHECK_CHANNEL_LABEL || "Installed Google Chrome";
  await page.getByRole("combobox", { name: "Separate browser" }).click();
  await page.getByRole("option", { name: label }).click();
  await page.getByRole("button", { name: /^Save/ }).click();
  await page.waitForTimeout(3000);
  await shot("browser-settings-saved");
  const chosen = await page.getByRole("combobox", { name: "Separate browser" }).textContent();
  console.error(JSON.stringify({ chosen }));
}
