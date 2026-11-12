import { expect, test } from '@playwright/test';

for (const width of [360, 390, 412]) {
  test(`mobile docs index remains complete and usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await page.emulateMedia({ colorScheme: width === 390 ? 'dark' : 'light' });
    await page.goto('/docs/');
    const index = page.getByRole('button', { name: 'Docs index', exact: true });
    await expect(index).toBeVisible();
    await expect(page.getByText('On this page', { exact: true }).filter({ visible: true })).toBeVisible();
    await index.click();
    const sidebar = page.locator('#starlight__sidebar');
    await expect(sidebar).toBeVisible();
    const lastLink = sidebar.getByRole('link', { name: 'Your first analysis' });
    await lastLink.scrollIntoViewIfNeeded();
    await expect(lastLink).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('mobile-docs-index.png') });
    await lastLink.click();
    await expect(page).toHaveURL(/\/docs\/examples\/check-a-dataset\//);
    await expect(sidebar).toBeHidden();
    await page.goto('/docs/');
    await page.locator('mobile-starlight-toc summary').click();
    await expect(page.locator('mobile-starlight-toc').getByRole('link', { name: 'Try one small investigation' })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath('mobile-page-index.png') });
  });
}

test('mobile gallery keeps the complete docs index without a page table of contents', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ['/docs/widgets/', '/docs/widgets/components/']) {
    await page.goto(route);
    const index = page.getByRole('button', { name: 'Docs index', exact: true });
    await expect(index).toBeVisible();
    await expect(page.locator('mobile-starlight-toc')).toHaveCount(0);
    const title = page.getByRole('heading', { level: 1 });
    const buttonBox = await index.boundingBox();
    const titleBox = await title.boundingBox();
    expect(buttonBox!.height).toBeGreaterThanOrEqual(32);
    expect(titleBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height);
    await index.click();
    const lastLink = page.locator('#starlight__sidebar').getByRole('link', { name: 'Your first analysis' });
    await lastLink.scrollIntoViewIfNeeded();
    await expect(lastLink).toBeInViewport();
    await page.keyboard.press('Escape');
    await expect(page.locator('#starlight__sidebar')).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.screenshot({ path: test.info().outputPath('mobile-gallery-index.png') });
});

test('worked examples share one docs entry point and old tutorial bookmarks still work', async ({ page }) => {
  await page.goto('/docs/examples/');
  const article = page.locator('.sl-markdown-content');
  for (const [name, route] of [
    ['Check a small dataset', '/docs/examples/check-a-dataset/'],
    ['Explore ground motion', '/docs/examples/ground-motion/'],
    ['Find a decision in research notes', '/docs/examples/research-notes/'],
    ['Create a report and presentation', '/docs/examples/reports-and-slides/'],
  ]) {
    await expect(article.getByRole('link', { name, exact: true })).toHaveAttribute('href', route);
  }
  for (const [oldRoute, newRoute] of [
    ['/tutorials/', '/docs/examples/'],
    ['/tutorials/guides/first-analysis/', '/docs/examples/ground-motion/'],
    ['/tutorials/guides/install-a-blueprint/', '/docs/agents-and-plugins/#pick-an-agent-for-a-conversation'],
  ]) {
    await page.goto(oldRoute);
    await expect(page).toHaveURL(new RegExp(`${newRoute}$`));
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  }
});

test('header and footer use real vector artwork on home and documentation pages', async ({ page }) => {
  for (const route of ['/', '/docs/']) {
    await page.goto(route);
    const logos = page.locator('a.sh-brand img, footer img');
    await expect(logos).toHaveCount(2);
    for (const logo of await logos.all()) {
      const src = await logo.getAttribute('src');
      expect(src).toMatch(/\.svg$/);
      const response = await page.request.get(src!);
      expect(response.ok()).toBe(true);
      const svg = await response.text();
      expect(svg).toContain('<path');
      expect(svg).not.toMatch(/<image\b|data:image\//);
    }
    await expect(page.getByRole('button', { name: 'Docs index', exact: true })).toBeHidden();
  }
});

test('download cards link the latest release installers and its notes', async ({ page }) => {
  const base = 'https://github.com/iowarp/clio-agent/releases/download/v0.9.4.24/';
  const exe = `${base}CLIO.Desktop_0.9.4.24_x64-bundled.exe`;
  const msi = `${base}CLIO.Desktop_0.9.4.24_x64_en-US-bundled.msi`;
  await page.route('https://api.github.com/repos/iowarp/clio-agent/releases/latest', (route) => route.fulfill({
    json: {
      tag_name: 'v0.9.4.24',
      assets: [
        { name: 'CLIO.Desktop_0.9.4.24_x64-bundled.exe', browser_download_url: exe },
        { name: 'CLIO.Desktop_0.9.4.24_x64_en-US-bundled.msi', browser_download_url: msi },
      ],
    },
  }));
  await page.goto('/#download');
  const downloads = page.getByRole('region', { name: 'Download CLIO', exact: true });
  const windows = downloads.locator('.dp-card').filter({ has: page.getByRole('heading', { name: 'Windows', exact: true }) });
  await expect(windows.getByRole('link', { name: 'Download installer', exact: true })).toHaveAttribute('href', exe);
  await expect(windows.getByRole('link', { name: 'Or download the .msi package', exact: true })).toHaveAttribute('href', msi);
  for (const name of ['macOS', 'Linux']) {
    await expect(downloads.getByRole('heading', { name, exact: true })).toBeVisible();
  }
  await expect(downloads.getByRole('link', { name: 'Release notes for 0.9.4.24', exact: true })).toHaveAttribute(
    'href',
    'https://github.com/iowarp/clio-agent/releases/tag/v0.9.4.24',
  );
  await downloads.screenshot({ path: test.info().outputPath('download-cards.png') });
});
