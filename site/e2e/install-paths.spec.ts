import { expect, test } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`current install guidance keeps the frozen TUI out of deployment at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/');
    await expect(page.getByText('The legacy terminal UI is frozen and unsupported.', { exact: false })).toBeVisible();
    await page.getByRole('tab', { name: 'Web and servers', exact: true }).click();
    await expect(page.getByRole('tabpanel', { name: 'Web and servers' })).toContainText('clio web');
    await page.screenshot({ path: test.info().outputPath('supported-install-paths.png'), fullPage: true });

    await page.goto('/docs/install/');
    await expect(page.getByText('The legacy TUI is frozen and unsupported as of 0.9.5.1.', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Desktop app', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('install-guide.png'), fullPage: true });

    const instructions = await request.get('/install.md');
    expect(instructions.ok()).toBe(true);
    expect(await instructions.text()).toContain('The legacy TUI is frozen and unsupported');
    expect(await instructions.text()).toContain('including from Claude Code');
  });
}
