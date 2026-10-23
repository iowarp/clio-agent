// Review background proposals on the memory page: accept, edit and accept, reject.
//   node scripts/cloud/ui_check.mjs --label memory-inbox --steps scripts/cloud/checks/memory_inbox.mjs
// INBOX_ACCEPT, INBOX_EDIT (+ INBOX_EDIT_TO) and INBOX_REJECT name the cards by their text.
const ACCEPT = process.env.INBOX_ACCEPT ?? "viridis";
const EDIT = process.env.INBOX_EDIT ?? "HDF5 I/O benchmarking";
const EDIT_TO = process.env.INBOX_EDIT_TO ?? "The user co-leads the HDF5 I/O benchmarking effort.";
const REJECT = process.env.INBOX_REJECT ?? "A. Patel";

export default async function memoryInbox(page, shot) {
  const base = new URL(page.url()).origin;
  await page.goto(`${base}/settings/memory`, { waitUntil: "networkidle" });
  const inbox = page.getByRole("list", { name: "Proposed changes" });
  await inbox.waitFor({ timeout: 30000 });
  await inbox.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  await shot("inbox");
  const card = (text) => inbox.getByRole("listitem").filter({ hasText: text });
  // Accept one change as proposed.
  await card(ACCEPT).getByRole("button", { name: "Accept", exact: true }).click();
  await page.getByText("Change applied").waitFor({ timeout: 15000 });
  // Correct one before accepting it.
  await card(EDIT).getByRole("button", { name: "Edit and accept" }).click();
  const editing = inbox.getByRole("listitem").filter({
    has: page.getByRole("textbox", { name: "Facts to apply, one per line" }),
  });
  await editing.getByRole("textbox", { name: "Facts to apply, one per line" }).fill(EDIT_TO);
  await shot("editing");
  await editing.getByRole("button", { name: "Apply edited" }).click();
  await page.waitForTimeout(1500);
  // Reject one.
  await card(REJECT).getByRole("button", { name: "Reject" }).click();
  await page.getByText("Change rejected").waitFor({ timeout: 15000 });
  await page.waitForTimeout(1500);
  await shot("after");
  await page.getByRole("heading", { name: "What is remembered" }).scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  await shot("documents");
}
