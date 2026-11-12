import { expect, test } from '@playwright/test';

test('home page keeps the download in reach and its demo and tabs working on desktop and mobile', async ({ page }) => {
	for (const width of [1440, 390]) {
		await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await page.goto('/');
		await page.evaluate(() => document.fonts.ready);
		await expect(page.locator('.h-hero .hd-main').first()).toBeInViewport();
		await expect(page.getByRole('link', { name: 'Read the documentation', exact: true })).toBeVisible();
		await page.screenshot({ path: test.info().outputPath(`home-${width}-light.png`) });
		if (width === 390) {
			await page.emulateMedia({ colorScheme: 'dark' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
			await page.screenshot({ path: test.info().outputPath('home-390-dark.png'), animations: 'disabled' });
		}
		expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);

		const demo = page.locator('.wsd');
		await expect(demo).toBeVisible();
		// The phone layout drops the composer and its model chip.
		if (width === 1440) await expect(demo.locator('.wsd-logo').filter({ visible: true }).first()).toBeVisible();
		const tools = demo.locator('.wsd-header-tools > *').filter({ visible: true });
		const tops = new Set<number>();
		for (const tool of await tools.all()) tops.add(Math.round((await tool.boundingBox())!.y));
		expect(tops.size).toBeLessThanOrEqual(1);

		const tablist = page.getByRole('tablist', { name: 'What CLIO can do' });
		await tablist.scrollIntoViewIfNeeded();
		await expect(tablist.locator('xpath=ancestor::astro-island')).not.toHaveAttribute('ssr', '');
		const first = tablist.getByRole('tab', { name: 'Ask your data', exact: true });
		await expect(first).toHaveAttribute('aria-selected', 'true');
		const panel = page.locator('#ct-panel');
		const before = await panel.locator('h3').textContent();
		const tools7 = tablist.getByRole('tab', { name: 'Your tools', exact: true });
		await tools7.click();
		await expect(tools7).toHaveAttribute('aria-selected', 'true');
		await expect(first).toHaveAttribute('aria-selected', 'false');
		await expect(panel.locator('h3')).not.toHaveText(before!);
		await tools7.focus();
		await page.keyboard.press('ArrowRight');
		const live = tablist.getByRole('tab', { name: 'Live experiments', exact: true });
		await expect(live).toHaveAttribute('aria-selected', 'true');
		await expect(live).toBeFocused();
		await page.screenshot({ path: test.info().outputPath(`capability-tabs-${width}.png`) });
	}
});

test('the evidence guide explains current controls and exposes artifact lineage', async ({ page }) => {
	await page.goto('/docs/memory-and-evidence/');
	await expect(page.getByRole('heading', {name:'Data and Work',exact:true})).toBeVisible();
	await page.getByRole('heading', {name:'Follow artifact lineage',exact:true}).scrollIntoViewIfNeeded();
	await expect(page.getByRole('img', {name:/^Artifact lineage graph with/})).toBeVisible();
	await expect(page.getByRole('main')).toContainText('it does not invent missing inputs');
	for (const image of await page.getByRole('main').getByRole('img').all()) {
		expect((await page.request.get((await image.getAttribute('src'))!)).ok()).toBe(true);
	}
	await page.screenshot({path:test.info().outputPath('evidence-lineage-guide.png')});
});

test('docs reveal the current guide group and distinguish account sign-in from workspace datasets', async ({ page }) => {
	await page.goto('/docs/');
	const filesGroup = page.locator('#starlight__sidebar details').filter({ has: page.getByRole('link', { name: 'Connect a data source', exact: true, includeHidden: true }) });
	await expect(filesGroup).not.toHaveAttribute('open', '');
	await page.getByRole('navigation', { name: 'Choose a CLIO guide' }).getByRole('link', { name: /Bring your data/ }).click();
	await expect(page).toHaveURL(/\/docs\/files\//);
	await expect(filesGroup).toHaveAttribute('open', '');
	await filesGroup.getByRole('link', { name: 'Connect a data source', exact: true }).click();
	await expect(page.getByRole('main')).toContainText('Signing in does not attach a dataset');
	await expect(page.getByRole('main')).toContainText('Files → Sources');
});

test('private research recordings and downloadable documents are absent', async ({ page }) => {
	for (const path of [
		'/media/opal1-document-review/OPAL1_research_meeting_report.docx',
		'/media/opal1-document-review/OPAL1_research_meeting_15min.pptx',
		'/media/opal1-document-review/report-overview-1.png',
		'/media/files-opal-report-review-current.mp4',
		'/media/files-opal-report-review.mp4',
		'/media/clio-session-export.mp4',
		'/media/source-context-recovery.png',
		'/media/source-accounts.png',
	]) expect((await page.request.get(path)).status()).toBe(404);
});
