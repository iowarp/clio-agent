// Review background proposals on the memory page: accept, edit and accept, reject.
//   node scripts/cloud/ui_check.mjs --label memory-inbox --steps scripts/cloud/checks/memory_inbox.mjs
export default async function memoryInbox(page, shot) {
  const base = new URL(page.url()).origin;
  await page.goto(`${base}/settings/memory`, { waitUntil: "networkidle" });
  const inbox = page.getByRole("list", { name: "Proposed changes" });
  await inbox.waitFor({ timeout: 30000 });
  await inbox.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  await shot("inbox");
  const card = (text) => inbox.getByRole("listitem").filter({ hasText: text });
  // Accept the colormap preference as proposed.
  await card("viridis").getByRole("button", { name: "Accept", exact: true }).click();
  await page.getByText("Change applied").waitFor({ timeout: 15000 });
  // Correct the benchmarking fact before accepting it.
  await card("HDF5 I/O benchmarking").getByRole("button", { name: "Edit and accept" }).click();
  const box = card("HDF5").getByRole("textbox", { name: "Facts to apply, one per line" });
  await box.fill("The user co-leads the HDF5 I/O benchmarking effort.");
  await shot("editing");
  await card("HDF5").getByRole("button", { name: "Apply edited" }).click();
  await page.waitForTimeout(1500);
  // Reject a fact about a colleague.
  await card("A. Patel").getByRole("button", { name: "Reject" }).click();
  await page.getByText("Change rejected").waitFor({ timeout: 15000 });
  await page.waitForTimeout(1500);
  await shot("after");
  await page.getByRole("heading", { name: "What is remembered" }).scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  await shot("documents");
}
