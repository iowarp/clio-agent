import { expect, test } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`current install guidance keeps the frozen TUI out of deployment at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/');
    await expect(page.getByText('The legacy terminal UI is frozen and unsupported.', { exact: false })).toBeVisible();
    const tab = page.getByRole('tab', { name: 'Web and servers', exact: true });
    await tab.scrollIntoViewIfNeeded();
    // This island hydrates when visible; wait for its event handlers before clicking.
    await expect(tab.locator('xpath=ancestor::astro-island')).not.toHaveAttribute('ssr', '');
    await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    const servers = page.getByRole('tabpanel').filter({ hasText: 'Install the service and Web' });
    await expect(servers).toBeVisible();
    await expect(servers).toContainText('clio web');
    await page.screenshot({ path: test.info().outputPath('supported-install-paths.png') });

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
