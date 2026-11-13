// Check (AQ6, AQ7, AQ12): the setup card inside the conversation. A message makes the
// agent call a tool on a connector that is not ready; the call waits and the card
// appears above the composer with the rows still missing. Then:
//   CHECK_ACTION=signin   sign in through the card (the fixture's consent page), and the
//                         same call continues with no new message from the person
//   CHECK_ACTION=decline  decline: the call returns the typed refusal naming the connector
//   CHECK_ACTION=fill     type CHECK_FILL ("Label=value;...") and press Continue
//   CHECK_ACTION=none     no card is expected: wait for the turn (AQ8, the agent sets a value)
// CHECK_PROMPT is the message (required). CHECK_SETTLE is the wait for the turn, in ms.
// It prints one JSON line: the session id, the card's title and rows, the outcome line.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);

async function turnIdle(page, sid, ms) {
  const agent = process.env.CHECK_AGENT || "http://127.0.0.1:8802";
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    const state = await fetch(`${agent}/v1/sessions/${sid}`)
      .then((r) => r.json())
      .catch(() => ({}));
    if (state.status === "idle") return true;
  }
  return false;
}

export default async function (page, shot) {
  const prompt = process.env.CHECK_PROMPT;
  if (!prompt) throw new Error("CHECK_PROMPT is required");
  const settleMs = Number(process.env.CHECK_SETTLE || 240000);
  const composer = page.locator('[data-slot="clio-composer"] [contenteditable="true"]').first();
  const posted = page.waitForRequest(
    (r) => r.method() === "POST" && /\/v1\/sessions\/[^/]+\/messages$/u.test(r.url()),
    { timeout: 30000 },
  );
  await composer.click();
  await page.keyboard.type(prompt);
  await page.keyboard.press("Enter");
  const sid = new URL((await posted).url()).pathname.split("/")[3];
  const card = page.locator('[data-slot="ask-card"][data-interaction-kind="connector_setup"]');
  // The session's tool permission rules come first: an external tool call asks once.
  const permission = page.locator('[data-slot="ask-card"][data-interaction-kind="permission"]');
  const deadline = Date.now() + settleMs;
  let allowed = 0;
  const action = process.env.CHECK_ACTION || "decline";
  const agent = process.env.CHECK_AGENT || "http://127.0.0.1:8802";
  const idle = async () =>
    (await fetch(`${agent}/v1/sessions/${sid}`).then((r) => r.json()).catch(() => ({}))).status ===
    "idle";
  while (Date.now() < deadline && !(await card.count())) {
    if (action === "none" && (await idle())) break;
    if (await permission.count()) {
      await shot(`permission-${++allowed}`);
      await permission.getByRole("button", { name: "Allow for this session" }).click();
    }
    await page.waitForTimeout(1500);
  }
  if (action === "none") {
    await settle(page, 3000);
    await shot("after-turn");
    console.error(JSON.stringify({ sid, permissions: allowed }));
    return;
  }
  await card.waitFor({ timeout: 5000 });
  await settle(page, 2500);
  await shot("setup-card");
  const report = {
    sid,
    title: await card.locator('[data-slot="pending-interaction-title"]').innerText(),
    rows: await card.locator("[data-need]").evaluateAll((items) =>
      items.map((item) => ({
        key: item.getAttribute("data-need"),
        kind: item.getAttribute("data-need-kind"),
        type: item.querySelector("input")?.type ?? null,
        text: item.innerText.replace(/\s+/gu, " ").trim(),
      })),
    ),
    inline: await page
      .locator('[data-slot="connector-setup-activity"]')
      .allInnerTexts()
      .catch(() => []),
  };

  if (action === "signin") {
    const popup = page.context().waitForEvent("page", { timeout: 60000 });
    await card.getByRole("button", { name: /^Sign in/u }).first().click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ timeout: 20000 });
    await shot("sign-in-dialog");
    await dialog.getByRole("button", { name: /^Sign in$/u }).click();
    const consent = await popup;
    await consent.waitForLoadState();
    await consent.getByRole("link", { name: "Allow test connection" }).click();
    await consent.waitForEvent("close", { timeout: 60000 }).catch(() => undefined);
  } else if (action === "fill") {
    for (const pair of (process.env.CHECK_FILL || "").split(";").filter(Boolean)) {
      const [label, ...rest] = pair.split("=");
      await card.getByLabel(label.trim(), { exact: true }).fill(rest.join("="));
    }
    await shot("setup-card-filled");
    await card.getByRole("button", { name: /Continue/u }).click();
  } else {
    await card.getByRole("button", { name: /Decline/u }).click();
  }
  report.turnFinished = await turnIdle(page, sid, settleMs);
  await settle(page, 3000);
  await shot(`after-${action}`);
  report.cardGone = (await card.count()) === 0;
  report.inlineAfter = await page
    .locator('[data-slot="connector-setup-activity"]')
    .allInnerTexts()
    .catch(() => []);
  console.error(JSON.stringify(report));
}
