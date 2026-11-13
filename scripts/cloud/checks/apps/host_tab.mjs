// AQ13, AQ14, AQ23, AQ24: opens a conversation and asks for the host panel fixture's view.
// The view asks for full screen and opens in a tab; it moves between card and tab
// with one frame each time and replays input and result; https and file: links;
// a download becomes a workspace file and an artifact. Also checks that switching
// workbench tabs does not reload an app tab.
import {
  check,
  expectText,
  frameCount,
  json,
  log,
  openConversation,
  sendAndSettle,
  sessionIdFrom,
  viewIn,
} from "./_app_view.mjs";

export default async function (page, shot) {
  await openConversation(page, process.env.CHECK_WORKSPACE || "host-panel");
  await sendAndSettle(page, "Open the host panel for the topic alpha.");
  const sid = sessionIdFrom(page);
  log("session", sid);
  const inline = viewIn(page, "inline");
  await expectText(inline.locator("#mode"), /^inline$/, 60000);
  await expectText(inline.locator("#modes"), /inline, fullscreen/);
  log("host features", await inline.locator("#caps").textContent());
  await expectText(inline.locator("#caps"), /downloadFile.*openLinks/);
  // The view is same-origin with its own sandbox host, which carries no API authority.
  const origin = await inline.locator("#origin").textContent();
  const storage = await expectText(inline.locator("#storage"), /\S/);
  // The probe's own refused request is the one expected 403 line in the console.
  await inline.locator("#probe").click();
  const api = await expectText(inline.locator("#api"), /HTTP|blocked/);
  log("view origin, storage, API", { origin, storage, api });
  check(/\.mcp-apps\.localhost:/.test(origin), "the view runs on its sandbox host");
  check(storage === "available", "the view can use its storage");
  check(/HTTP 403 sandbox_origin_refused/.test(api), "the sandbox host refuses the API");
  await shot("inline");

  // AQ13: the view requests full screen.
  await inline.locator("#fullscreen").click();
  await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
  const tab = viewIn(page, "tab");
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  await expectText(tab.locator("#topic"), /\S/);
  await page.locator("[data-mcp-app-in-tab]").first().waitFor({ timeout: 10000 });
  check((await frameCount(page)) === 1, "one frame while in the tab");
  const app = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab");
  const record = await json(`/v1/sessions/${sid}/mcp-apps/${app}?data_ref=${await dataRef(page)}`, {
    headers: { Referer: page.url() },
  });
  log("descriptor after fullscreen", { status: record.status, mode: record.body?.display_mode });
  check(record.body?.display_mode === "fullscreen", "the record is fullscreen");
  await shot("tab");

  // AQ14: Return to conversation, then Open in tab.
  const loadedInTab = await tab.locator("#loaded").textContent();
  await page.getByRole("button", { name: "Return to conversation" }).click();
  await expectText(inline.locator("#mode"), /^inline$/);
  await expectText(inline.locator("#topic"), /\S/);
  await expectText(inline.locator("#result"), /\d{4}-/);
  check((await page.locator("[data-mcp-app-tab]").count()) === 0, "the tab closed");
  check((await frameCount(page)) === 1, "one frame back in the card");
  log("reloaded in card", { loadedInTab, loadedInline: await inline.locator("#loaded").textContent() });
  await shot("returned");
  await page.getByRole("button", { name: "Open in tab" }).click();
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  await expectText(tab.locator("#result"), /\d{4}-/);
  check((await frameCount(page)) === 1, "one frame in the tab again");
  check((await page.locator("[data-mcp-app-in-tab]").count()) === 1, "the card shows the in-tab row");
  await shot("tab-again");

  // AQ23: an https link opens a browser page; file: is refused with a reason.
  const loaded = await tab.locator("#loaded").textContent();
  await tab.locator("#link-https").click();
  await expectText(tab.locator("#status"), /link opened|refused/);
  log("https link", await tab.locator("#status").textContent());
  await page.getByRole("tab", { name: /Example Domain|example\.org/ }).first().waitFor({ timeout: 30000 });
  const pages = await json(`/v1/sessions/${sid}/browser/pages`);
  log("browser pages", (pages.body?.pages ?? pages.body ?? []).map?.((p) => p.url ?? p.host));
  await shot("link-opened");
  // Back on the app tab, the view did not reload while the browser tab was shown.
  await page.getByRole("tab", { name: /Open Panel|open_panel/ }).first().click();
  await expectText(tab.locator("#mode"), /^fullscreen$/);
  check((await tab.locator("#loaded").textContent()) === loaded, "no reload on tab switch");
  await tab.locator("#link-file").click();
  const refused = await expectText(tab.locator("#status"), /refused/);
  check(/unsupported_scheme/.test(refused), "file: refused with unsupported_scheme");
  log("file link", refused);
  await shot("file-refused");

  // AQ24: a download is a workspace file and an artifact.
  await tab.locator("#download").click();
  const saved = await expectText(tab.locator("#status"), /saved: .*\.csv|refused: (?!Only http)/);
  log("download", saved);
  check(/saved: /.test(saved), "the download was saved");
  const artifacts = await json(`/v1/sessions/${sid}/artifacts`);
  const names = JSON.stringify(artifacts.body);
  log("artifact listed", /panel-samples/.test(names));
  check(/panel-samples/.test(names), "the saved file is listed as an artifact");
  await page.waitForTimeout(1500);
  await shot("downloaded");
}

async function dataRef(page) {
  const src = await page.locator("[data-mcp-app-tab] [data-mcp-app-iframe]").first().getAttribute("src");
  return new URL(src).searchParams.get("data_ref");
}
