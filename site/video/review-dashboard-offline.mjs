import puppeteer from "puppeteer";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Reopen an actual UI download in a fresh browser with networking disabled.
const [filename, destination] = process.argv.slice(2);
if (!filename || !destination)
  throw new Error("Supply downloaded HTML and evidence directory");
await mkdir(destination, { recursive: true });
const browser = await puppeteer.launch({
  headless: true,
  executablePath: process.env.DEMO_CHROME_PATH,
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1000 });
  const errors = [];
  const networkAttempts = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("requestfailed", (request) =>
    networkAttempts.push({
      url: request.url(),
      error: request.failure()?.errorText,
    }),
  );
  await page.setOfflineMode(true);
  await page.goto(pathToFileURL(resolve(filename)).href, {
    waitUntil: "load",
    timeout: 45000,
  });
  await page.waitForFunction(
    () =>
      Boolean(
        document.querySelector('[data-slot="a2ui-chart-view"]')?.__clioChart,
      ),
    { timeout: 30000 },
  );
  if (
    await page.$$eval(
      '[data-slot="a2ui-dashboard"] [role="tab"]',
      (nodes) => nodes.length,
    )
  )
    throw new Error(
      "The composed overview should not hide its evidence in tabs",
    );
  await page.waitForSelector('button[aria-label="Select Fernhill"]', {
    timeout: 30000,
  });
  const points = await page.$$eval(
    '[data-slot="a2ui-map-surface"] button[aria-label^="Select "]',
    (nodes) => nodes.map((node) => node.getAttribute("aria-label")),
  );
  if (points.length !== 5)
    throw new Error(`Offline map has ${points.length} station markers`);
  await page.waitForSelector("tbody tr", { timeout: 15000 });
  const rows = await page.$$eval("tbody tr", (nodes) =>
    nodes
      .filter((node) => node.querySelectorAll("td").length === 13)
      .map((node) =>
        [...node.querySelectorAll("td")].map((cell) => cell.textContent.trim()),
      ),
  );
  const expected = {
    Fernhill: [24, 21, 16, 4, 0, 0, 0, -21],
    Orchard: [18, 18, 15, 8, 5, 3, 2, -16],
    "Canal Street": [24, 3, 8, 20, 24, 24, 24, 21],
    "Market Square": [18, 2, 5, 12, 15, 17, 18, 16],
    Park: [20, 10, 10, 10, 10, 10, 10, 0],
  };
  if (rows.length !== 5)
    throw new Error(`Offline evidence has ${rows.length} rows`);
  for (const row of rows) {
    if (
      JSON.stringify(row.slice(2, 10).map(Number)) !==
      JSON.stringify(expected[row[0]])
    )
      throw new Error(`Offline counts changed: ${row[0]}`);
  }
  const geometry = () =>
    page.$$eval('[data-slot="a2ui-frame"]', (nodes) =>
      nodes.map((node) => {
        const r = node.getBoundingClientRect();
        return {
          heading: node.textContent.slice(0, 80),
          x: r.x,
          y: r.y,
          width: r.width,
          height: r.height,
        };
      }),
    );
  const wide = await geometry();
  if (wide.length < 4) {
    await page.screenshot({
      path: resolve(destination, "unexpected-layout.png"),
      fullPage: true,
    });
    const slots = await page.$$eval("[data-slot]", (nodes) => [
      ...new Set(nodes.map((node) => node.dataset.slot)),
    ]);
    throw new Error(
      `Offline report has ${wide.length} frames: ${JSON.stringify(slots)}`,
    );
  }
  const ratio = wide[0].width / wide[1].width;
  if (Math.abs(ratio - 2) > 0.15 || Math.abs(wide[0].y - wide[1].y) > 1)
    throw new Error(
      `Primary row lost its 2:1 proportions: ${JSON.stringify(wide.slice(0, 2))}`,
    );
  if (
    Math.abs(wide[2].width / wide[3].width - 1) > 0.05 ||
    Math.abs(wide[2].y - wide[3].y) > 1
  )
    throw new Error("Secondary row lost its equal panel widths");
  await page.screenshot({
    path: resolve(destination, "composed-wide.png"),
    fullPage: true,
  });
  await page.click('button[aria-label="Select Fernhill"]');
  await page
    .waitForFunction(
      () =>
        document.querySelectorAll('tbody tr[aria-selected="true"]').length ===
        2,
      { timeout: 5000 },
    )
    .catch(async (error) => {
      const state = await page.evaluate(() => ({
        rows: [...document.querySelectorAll("tbody tr")].map((node) => ({
          name: node.querySelector("td")?.textContent,
          selected: node.getAttribute("aria-selected"),
        })),
        charts: [
          ...document.querySelectorAll('[data-slot="a2ui-chart-view"]'),
        ].map((node) => node.__clioChart.view.data("sel_store")),
        selectedButtons: [
          ...document.querySelectorAll('[aria-pressed="true"]'),
        ].map((node) => node.getAttribute("aria-label")),
      }));
      throw new Error(`${error}: ${JSON.stringify(state)}`);
    });
  const selected = await page.$$eval(
    'tbody tr[aria-selected="true"]',
    (nodes) =>
      nodes.map((node) => node.querySelector("td")?.textContent.trim()),
  );
  if (selected.some((name) => name !== "Fernhill"))
    throw new Error(`Incorrect linked rows: ${selected}`);
  const chartSelections = await page.$$eval(
    '[data-slot="a2ui-chart-view"]',
    (nodes) =>
      nodes.map((node) =>
        node.__clioChart.view.data("sel_store").map((tuple) => tuple.values),
      ),
  );
  if (
    chartSelections.length !== 2 ||
    chartSelections.some((values) => JSON.stringify(values) !== '[["fern"]]')
  )
    throw new Error(
      `Map selection did not reach both charts: ${JSON.stringify(chartSelections)}`,
    );
  await page.screenshot({
    path: resolve(destination, "linked-fernhill.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 660, height: 1000 });
  await page
    .waitForFunction(
      () => {
        const frames = [
          ...document.querySelectorAll('[data-slot="a2ui-frame"]'),
        ];
        return (
          frames.length >= 4 &&
          frames[1].getBoundingClientRect().y >
            frames[0].getBoundingClientRect().y + 100
        );
      },
      { timeout: 5000 },
    )
    .catch(async (error) => {
      await page.screenshot({
        path: resolve(destination, "unexpected-narrow.png"),
        fullPage: true,
      });
      const sizes = await page.$$eval(
        'main,[data-slot="a2ui-dashboard"],[data-slot="a2ui-surface-root"],[data-slot="a2ui-frame"]',
        (nodes) =>
          nodes.map((node) => ({
            tag: node.tagName,
            slot: node.dataset.slot,
            width: node.getBoundingClientRect().width,
            container: getComputedStyle(node).containerType,
            parentStyle: node.parentElement.getAttribute("style"),
            parentWrap: getComputedStyle(node.parentElement).flexWrap,
            flex: getComputedStyle(node).flex,
          })),
      );
      throw new Error(
        `${error}: ${JSON.stringify({ sizes, frames: await geometry() })}`,
      );
    });
  const narrow = await geometry();
  if (
    narrow
      .slice(0, 4)
      .some((frame) => Math.abs(frame.width - narrow[0].width) > 1)
  )
    throw new Error("Narrow analytical panels should use the available width");
  await page.screenshot({
    path: resolve(destination, "composed-narrow.png"),
    fullPage: true,
  });
  await writeFile(
    resolve(destination, "review.json"),
    JSON.stringify(
      {
        filename,
        networking: "disabled",
        tabs: 0,
        stationMarkers: points,
        rows,
        wide,
        narrow,
        selected,
        chartSelections,
        errors,
        networkAttempts,
      },
      null,
      2,
    ),
  );
  if (errors.length)
    throw new Error(`Offline renderer errors: ${errors.join("; ")}`);
  console.log(
    JSON.stringify({
      tabs: 0,
      stations: points.length,
      evidenceRows: rows.length,
      sourceCounts: "unchanged",
      linkedCharts: chartSelections.length,
      primaryWidthRatio: ratio,
      networkAttempts: networkAttempts.length,
    }),
  );
} finally {
  await browser.close();
}
