import { expect, test } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`knowledge source guide and examples work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.goto('/docs/files/');
    const guides = page.getByRole('navigation', { name: 'Files and sources guides' });
    await guides.getByRole('link', { name: 'Knowledge source', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Knowledge source', level: 1 })).toBeVisible();
    await expect(page.locator('.sl-markdown-content')).toContainText('fictional');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    const captures = page.locator('.guide-screenshot');
    await expect(captures).toHaveCount(8);
    await expect(page.locator('.sl-markdown-content')).toContainText('Create a new vault');
    await expect(page.locator('.sl-markdown-content')).toContainText('Attach an existing vault or folder');
    await expect(page.locator('.sl-markdown-content')).toContainText('Obsidian app does not need to be installed');
    await expect(page.locator('.sl-markdown-content')).toContainText('All notes');
    await expect(page.locator('.sl-markdown-content')).toContainText('Isolated notes');
    for (const capture of await captures.all()) {
      await capture.scrollIntoViewIfNeeded();
      const image = capture.getByRole('img');
      await expect(image).toBeVisible();
      await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(600);
      const enlarged = capture.getByRole('link', { name: /^Enlarge:/ });
      const response = await page.request.get((await enlarged.getAttribute('href'))!);
      expect(response.ok()).toBe(true);
      expect(response.headers()['content-type']).toContain('image/jpeg');
    }
    const example = page.getByRole('link', { name: 'download the example vault', exact: true });
    const archive = await page.request.get((await example.getAttribute('href'))!);
    expect(archive.ok()).toBe(true);
    expect((await archive.body()).subarray(0, 2).toString()).toBe('PK');
    await page.getByRole('heading', { name: 'Knowledge source', level: 1 }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: test.info().outputPath(`knowledge-${width}.png`) });
  });
}
