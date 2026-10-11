import { expect, test } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`long-running guide shows controls and real evidence at ${width}px`, async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('starlight-theme', 'light'));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/docs/long-running-work/');
    await expect(page.getByRole('heading', { level: 1, name: 'Goals and long-running work' })).toBeVisible();
    for (const [index, title] of [
      'Completed three-checkpoint goal',
      'Active goal and hourly loop',
      'Completed paced goal and stopped loop',
      'Loop waiting for a human answer',
    ].entries()) {
      const button = page.getByRole('button', { name: new RegExp(`^View larger: ${title}`) });
      await button.scrollIntoViewIfNeeded();
      await expect.poll(() => button.getByRole('img').evaluate(element => (element as HTMLImageElement).complete && (element as HTMLImageElement).naturalWidth > 0)).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await button.click();
      const dialog = page.getByRole('dialog');
      await expect(dialog.getByRole('img')).toBeVisible();
      await expect.poll(() => dialog.getByRole('img').evaluate(element => (element as HTMLImageElement).complete && (element as HTMLImageElement).naturalWidth > 0)).toBe(true);
      await page.screenshot({ path: test.info().outputPath(`long-running-${index}-${width}.png`), animations: 'disabled' });
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      await expect(button).toBeFocused();
    }
    await page.goto('/docs/sessions/');
    await page.getByRole('link', { name: 'Goals and long-running work', exact: true }).last().click();
    await expect(page).toHaveURL(/\/docs\/long-running-work\/$/);
  });
}
