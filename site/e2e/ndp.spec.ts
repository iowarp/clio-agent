import { expect, test } from '@playwright/test';

for (const theme of ['light', 'dark']) {
  for (const width of [390, 1440]) {
    test(`NDP guides and real captures work at ${width}px in ${theme}`, async ({ page }) => {
      await page.addInitScript((value) => localStorage.setItem('starlight-theme', value), theme);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.goto('/docs/files/ndp/');
      const logo = page.getByRole('img', { name: 'National Data Platform', exact: true }).filter({ visible: true });
      await expect(logo).toHaveCount(1);
      expect(await logo.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      await expect(page).toHaveURL(/\/docs\/files\/ndp\/$/);
      await expect(page.getByRole('heading', { level: 1, name: 'Work with National Data Platform' })).toBeVisible();
      await expect(page.locator('.starlight-aside')).toContainText('not in a published installer');
      const capture = page.getByRole('button', { name: /^View larger: NDP dataset search/ });
      await capture.scrollIntoViewIfNeeded();
      await capture.click();
      const dialog = page.getByRole('dialog');
      const image = dialog.getByRole('img');
      await expect(image).toBeVisible();
      await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      await expect(capture).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: test.info().outputPath(`ndp-guide-${width}-${theme}.png`), fullPage: true });

      await page.locator('.sl-markdown-content').getByRole('link', { name: 'EarthScope investigation guide', exact: true }).click();
      await expect(page.getByRole('heading', { level: 1, name: 'Analyze EarthScope data from NDP' })).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Establish the historical coverage', exact: true })).toBeVisible();
      await expect(page.getByRole('img', { name: /^Three panels charting/ })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: test.info().outputPath(`ndp-example-${width}-${theme}.png`) });
      const media = await (await page.request.get('/media/guides.json')).json();
      const ndp = media.guides.find((guide: { id: string }) => guide.id === 'docs/files/ndp');
      expect(ndp.availability).toBe('development');
      expect(ndp.media).toEqual([
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual NDP catalog/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual Alta-Collins search/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^NDP dataset search/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual Endpoint setup/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual native Endpoint stream capture/) }),
      ]);
      const streams = media.guides.find((guide: { id: string }) => guide.id === 'docs/files/ndp-streams');
      expect(streams.media).toEqual([
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual exported scientific map/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual native Endpoint stream capture/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual historical replay form/) }),
        expect.objectContaining({ kind: 'image', alt: expect.stringMatching(/^Actual completed stream sample/) }),
      ]);
      const example = media.guides.find((guide: { id: string }) => guide.id === 'docs/examples/ndp-earthscope');
      expect(example).toBeUndefined();
      for (const item of [...ndp.media, ...streams.media]) {
        expect((await page.request.get(item.src)).ok()).toBe(true);
      }
    });
  }
}
