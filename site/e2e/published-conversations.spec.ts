import { expect, test } from '@playwright/test';

for (const [slug, title, messages] of [
  ['alta', /Alta seasonal/, 6],
  ['tahoe', /Tahoe/i, 6],
  ['konza', /Konza/i, 6],
  ['endpoint', /Manage scientific data through an Endpoint/, 14],
] as const) {
  test(`${slug}: public native archive renders saved results without contacting services`, async ({ page, baseURL }) => {
    // Self-contained exports carry 40-81 MB of real snapshots and need time to hydrate.
    test.setTimeout(60_000);
    const errors: string[] = [];
    const externalRequests: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== new URL(baseURL!).origin)
        externalRequests.push(request.url());
    });
    const response = await page.goto(`/conversations/ndp/${slug}/`);
    expect(response?.status()).toBe(200);
    expect(response?.headers()['content-type']).toContain('text/html');
    await expect(page.getByRole('tab', { name: 'Results', exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { level: 1, name: title }).filter({ visible: true })).toBeVisible();
    await expect(page.getByText(new RegExp(`Transcript.*${messages} messages`)).filter({ visible: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Results', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Saved results', exact: true })).toBeVisible();
    if (slug === 'alta') {
      const monthly = page.getByRole('region', { name: 'Interactive surface, Chart', exact: true }).filter({ hasText: 'Historical baseline' });
      await monthly.getByRole('button', { name: 'All complete months (17 Oct; 24 Nov-Apr)', exact: true }).click();
      await expect(monthly.getByRole('button', { name: 'All complete months (17 Oct; 24 Nov-Apr)', exact: true })).toHaveAttribute('aria-pressed', 'true');
      await expect(monthly.getByRole('img', { name: 'Monthly snowfall and historical spread: 7 rows', exact: true })).toBeVisible();
    }
    await page.screenshot({ path: test.info().outputPath(`${slug}-native-results.png`) });
    await page.getByRole('tab', { name: 'Evidence & skills', exact: true }).click();
    await expect(page.getByRole('tab', { name: 'Evidence & skills', exact: true })).toHaveAttribute('aria-selected', 'true');
    expect(errors).toEqual([]);
    expect(externalRequests).toEqual([]);
  });
}

for (const [path, title, messages] of [
  ['/conversations/delegation/reading-guide/', 'Reading guide to the design folder', 2],
  ['/conversations/delegation/skill-review/', 'Skill review across plugins', 8],
  ['/conversations/delegation/agent-fork.html', 'Agent fork check', 10],
  ['/conversations/studies/wordle/', 'Wordle study', 21],
] as const) {
  test(`${path}: published archive opens without contacting services`, async ({ page, baseURL }) => {
    // The reading guide embeds its ten children's transcripts (87 MB); the study, every branch's
    // (48 MB). Hydrating 87 MB takes over 30 s on a loaded machine.
    test.setTimeout(150_000);
    const errors: string[] = [];
    const externalRequests: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      if (/^https?:/.test(request.url()) && new URL(request.url()).origin !== new URL(baseURL!).origin)
        externalRequests.push(request.url());
    });
    const response = await page.goto(path);
    expect(response?.status()).toBe(200);
    expect(response?.headers()['content-type']).toContain('text/html');
    await expect(page.getByRole('heading', { level: 1, name: title, exact: true }).filter({ visible: true })).toBeVisible({ timeout: 90_000 });
    await expect(page.getByText(new RegExp(`Transcript.*${messages} messages`)).filter({ visible: true })).toBeVisible();
    expect(errors).toEqual([]);
    expect(externalRequests).toEqual([]);
  });
}

test('unqualified Ares archive is unpublished', async ({ page }) => {
  const response = await page.goto('/conversations/ndp/ares/');
  expect(response?.status()).toBe(404);
});

test('saved Tahoe geometry renders in a restricted-origin archive frame', async ({ page }) => {
  test.setTimeout(60_000);
  await page.route('**/__archive-map-origin-test', route => route.fulfill({
    contentType: 'text/html',
    body: '<!doctype html><title>Archive map regression</title><iframe title="Saved conversation" sandbox="allow-scripts" style="width:100%;height:900px" src="/conversations/ndp/tahoe/#results"></iframe>',
  }));
  await page.goto('/__archive-map-origin-test');
  const frame = page.frameLocator('iframe');
  await expect(frame.getByRole('heading', { name: 'Saved results', exact: true })).toBeVisible({ timeout: 20_000 });
  const surface = frame.locator('[data-slot="a2ui-map-surface"]').first();
  await expect.poll(() => surface.evaluate(node => {
    const map = (node as HTMLElement & { __clioMap?: {
      getLayer(id: string): unknown;
      queryRenderedFeatures(options: { layers: string[] }): unknown[];
    } }).__clioMap;
    const layer = 'clio-map-geometry-fill';
    return map?.getLayer(layer) ? map.queryRenderedFeatures({ layers: [layer] }).length : 0;
  }), { timeout: 20_000 }).toBeGreaterThan(0);
  await surface.screenshot({ path: test.info().outputPath('tahoe-restricted-origin-map.png') });
});
