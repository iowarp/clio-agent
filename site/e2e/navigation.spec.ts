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
    await expect(page).toHaveURL(/\/tutorials\/guides\/first-analysis\//);
    await expect(sidebar).toBeHidden();
    await page.goto('/docs/');
    await page.locator('mobile-starlight-toc summary').click();
    await expect(page.locator('mobile-starlight-toc').getByRole('link', { name: 'Next steps' })).toBeVisible();
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

test('header and footer use real vector artwork on home and documentation pages', async ({ page }) => {
  for (const route of ['/', '/docs/']) {
    await page.goto(route);
    const logos = page.locator('a.site-title img, footer img');
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
