import { expect, test } from '@playwright/test';

test('overview and documentation scale readably on large displays without horizontal overflow', async ({ page }, testInfo) => {
  for (const theme of ['light', 'dark']) {
    await page.addInitScript((value) => localStorage.setItem('starlight-theme', value), theme);
    for (const width of [390, 1280, 1920, 2560, 3440, 3840, 5120]) {
      await page.setViewportSize({ width, height: width >= 3440 ? 1440 : 900 });
      for (const route of ['/', '/docs/working-with-files/']) {
        await page.goto(route);
        await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
        const rootSize = await page.locator('html').evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize));
        const expected = Math.min(24, Math.max(16, 8 + width / 240));
        expect(rootSize).toBeCloseTo(expected, 1);
        expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
        if (route.startsWith('/docs/')) {
          const article = page.locator('.sl-markdown-content');
          const paragraph = article.locator('p').first();
          const fontSize = await paragraph.evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize));
          expect(fontSize).toBeGreaterThanOrEqual(rootSize);
          expect((await article.boundingBox())!.width / fontSize).toBeLessThan(70);
        }
        if (width === 3840 || width === 390) {
          await page.screenshot({ path: testInfo.outputPath(`${theme}-${route === '/' ? 'overview' : 'docs'}-${width}.png`) });
        }
      }
    }
  }
});
