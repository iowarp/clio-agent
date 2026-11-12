import { expect, test } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`install guidance offers the desktop installer and keeps the frozen TUI out of deployment at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/#download');
    const downloads = page.getByRole('region', { name: 'Download CLIO', exact: true });
    const command = downloads.getByLabel(/^Install command for /);
    await command.scrollIntoViewIfNeeded();
    await expect(command).toContainText(/https:\/\/clio\.iowarp\.ai\/desktop\.(sh|ps1)/);
    await expect(page.locator('main')).not.toContainText(/terminal UI|\bTUI\b/);
    await page.screenshot({ path: test.info().outputPath('home-install-command.png') });

    await page.goto('/docs/install/');
    await expect(page.getByText('The legacy TUI is frozen and unsupported as of 0.9.5.1.', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Desktop app', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath('install-guide.png'), fullPage: true });

    const instructions = await request.get('/install.md');
    expect(instructions.ok()).toBe(true);
    expect(await instructions.text()).toContain('The legacy TUI is frozen and unsupported');
    expect(await instructions.text()).toContain('including from Claude Code');
    const index = await request.get('/llms.txt');
    expect(index.ok()).toBe(true);
    expect(await index.text()).toContain('frozen and unsupported as of 0.9.5.1');
  });
}
