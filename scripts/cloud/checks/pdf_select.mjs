// A paper's PDF pane takes a touch or keyboard selection as its own pdf-quad mark.
//   CAP_WS=ws_... CAP_SID=sess_... CAP_OPEN="Draining Burst Buffers" PDF_GESTURE=touch|keyboard \
//     node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/pdf_select.mjs --label pdf-touch \
//     --width 390 --height 844 --touch
// It opens the paper from the conversation, shows the PDF, selects words in the PDF's text
// layer and ends the gesture the way a finger or Shift+arrow would; then it shoots the draft.

const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

export default async function pdfSelect(page, shot) {
  const { CAP_WS: ws, CAP_SID: sid, CAP_OPEN: open, PDF_GESTURE: gesture = "touch" } = process.env;
  const marks = [];
  page.on("request", (request) => {
    const body = request.postData() || "";
    if (body.includes('"profile"')) marks.push(body.slice(0, 400));
  });
  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "load" });
  await page.waitForTimeout(6000);
  await page.getByText(open, { exact: false }).last().click();
  await page.getByText("Outline", { exact: true }).last().waitFor({ timeout: 30000 });
  // The view switch (Source, PDF, Outline): the visible control whose text is PDF.
  const controls = await page.evaluate(() =>
    [...document.querySelectorAll("button, [role]")]
      .filter((node) => /PDF/.test(node.textContent || "") && (node.textContent || "").length < 40)
      .map((node) => [node.tagName, node.getAttribute("role"), node.getAttribute("data-state"), node.getBoundingClientRect().width > 0]),
  );
  console.log(JSON.stringify({ controls }));
  await page.locator("button, [role=tab], [role=radio]").filter({ hasText: /^PDF$/ }).locator("visible=true").first().click();
  await page.waitForTimeout(5000);
  // Open the comment session (text mode is its default), as a person taps Comment.
  await page.locator('[data-comment-toggle="true"]').locator("visible=true").first().click();
  await page.waitForTimeout(1000);
  await shot("pdf");
  console.log(JSON.stringify(await page.evaluate(() => {
    const hosts = [...document.querySelectorAll('[data-comment-text="own"]')];
    return hosts.map((host) => ({
      pages: host.querySelectorAll("[data-page]").length,
      spans: host.querySelectorAll("[data-page] span").length,
      text: [...host.querySelectorAll("[data-page] span")].slice(0, 4).map((n) => n.textContent),
      layers: [...host.querySelectorAll("[class*=textLayer], [class*=text-layer]")].length,
    }));
  })));
  const selected = await page.evaluate((how) => {
    // The visible viewer: the wide side-by-side layout is in the page but hidden at 390.
    const host = [...document.querySelectorAll('[data-comment-text="own"]')].find(
      (node) => node.getClientRects().length > 0,
    );
    const span = [...(host?.querySelectorAll("[data-page] span") ?? [])].find(
      (node) => (node.textContent || "").trim().split(/\s+/).length >= 3,
    );
    if (!host || !span) return "";
    const chain = [];
    for (let node = span; node && node !== document.body; node = node.parentElement) {
      const style = getComputedStyle(node);
      chain.push([node.tagName, node.className?.toString().slice(0, 40), style.userSelect, style.webkitUserSelect]);
    }
    window.__chain = chain;
    const range = document.createRange();
    range.selectNodeContents(span);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    const text = selection.toString();
    if (how === "keyboard") {
      host.dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowRight", shiftKey: true, bubbles: true }));
    } else {
      host.dispatchEvent(new TouchEvent("touchend", { bubbles: true, cancelable: true }));
    }
    return text;
  }, gesture);
  console.log(JSON.stringify({ gesture, selected, chain: await page.evaluate(() => window.__chain) }));
  await page.waitForTimeout(1500);
  await shot(`${gesture}-selected`);
  const toggle = await page.locator('[data-comment-toggle="true"]').locator("visible=true").first().innerText();
  console.log(JSON.stringify({ toggle }));
  console.log(JSON.stringify({ marks }));
}
