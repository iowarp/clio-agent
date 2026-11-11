import { expect, test } from '@playwright/test';

test('product showcase supports keyboard selection and usable image dialogs on desktop and mobile', async ({ page }) => {
	for (const width of [1440, 390]) {
		await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
		await page.emulateMedia({ colorScheme: 'light' });
		await page.goto('/');
		await page.evaluate(() => document.fonts.ready);
		await page.screenshot({ path: test.info().outputPath(`home-${width}-light.png`) });
		if (width === 390) {
			await page.emulateMedia({ colorScheme: 'dark' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
			await page.screenshot({ path: test.info().outputPath('home-390-dark.png'), animations: 'disabled' });
		}
		const showcase = page.getByRole('region', { name: 'Inside the CLIO workspace' });
		const dashboards = showcase.getByRole('tab', { name: 'Dashboards', exact: true });
		await expect(dashboards).toHaveAttribute('aria-selected', 'true');
		await dashboards.focus();
		await page.keyboard.press('ArrowRight');
		await expect(showcase.getByRole('tab', { name: 'Evidence', exact: true })).toHaveAttribute('aria-selected', 'true');
		await expect(showcase.getByRole('heading', { name: 'Find the files behind the result.' })).toBeVisible();
		await showcase.getByRole('tab', { name: 'Files', exact: true }).click();
		const enlarge = showcase.getByRole('button', { name: /^View larger:/ });
		await enlarge.click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toBeVisible();
		await expect(dialog).toContainText('The report distinguishes dock counts from trips');
		const bounds = await dialog.boundingBox();
		const viewport = page.viewportSize()!;
		expect(bounds!.x).toBeGreaterThanOrEqual(0);
		expect(bounds!.y).toBeGreaterThanOrEqual(0);
		expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
		expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
		await expect(dialog.getByRole('button', { name: 'Close', exact: true })).toBeInViewport();
		await page.screenshot({ path: test.info().outputPath(`figure-dialog-${width}.png`) });
		await page.keyboard.press('Escape');
		await expect(dialog).toBeHidden();
		await expect(enlarge).toBeFocused();
		expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
		const provenance = page.getByRole('region', {name:'A result has a history.'});
		await expect(provenance.getByRole('img')).toBeVisible();
		await expect(provenance).toContainText('Missing relationships are not inferred');
		await provenance.scrollIntoViewIfNeeded();
		await provenance.screenshot({path:test.info().outputPath(`provenance-${width}.png`)});
		await provenance.getByRole('button', {name:/^View larger:/}).click();
		await expect(dialog.getByRole('img')).toHaveAttribute('alt', /artifact lineage graph connecting bike-stations\.csv/);
		await expect(dialog.getByRole('link', {name:'Open full-size image (new tab)'})).toBeVisible();
		await page.screenshot({path:test.info().outputPath(`lineage-dialog-${width}.png`)});
		await page.keyboard.press('Escape');
		await dashboards.click();
		const reportEnlarge = showcase.getByRole('button', { name: /^View larger:/ });
		await reportEnlarge.click();
		await expect(dialog).toBeVisible();
		await expect(dialog.getByRole('img')).toHaveAttribute('alt', /complete five-station table/);
		await expect(dialog).toContainText('A shared selection connects the views.');
		await expect(dialog.getByRole('button', { name: 'Close', exact: true })).toBeInViewport();
		await page.screenshot({ path: test.info().outputPath(`report-dialog-${width}.png`) });
		await page.keyboard.press('Escape');
		await expect(reportEnlarge).toBeFocused();
		await showcase.getByRole('tab', { name:'Files',exact:true }).click();
		await showcase.getByRole('link', { name: 'Explore file workflows' }).click();
		await expect(page).toHaveURL(/\/docs\/examples\/reports-and-slides\/$/);
		await expect(page.getByRole('heading', { name: 'Create a report and presentation', exact: true })).toBeInViewport();
		await expect(page.getByRole('main').locator('video')).toHaveCount(4);
		for (const name of ['files-sensor-word-generation', 'files-sensor-slide-generation']) {
			await expect(page.locator(`video:has(source[src="/media/${name}.mp4"])`)).toBeVisible();
			const response = await page.request.get(`/media/${name}.mp4`);
			expect(response.ok()).toBe(true);
			expect(response.headers()['content-type']).toContain('video/mp4');
		}
		await expect(page.getByRole('main')).toContainText('CLIO creates the files during the recorded runs');
		for (const filename of ['Sensor_Summary_Team_Report.docx', 'Sensor_Summary_Team_Report.pdf', 'sensor_team_briefing.pptx', 'sensor_team_briefing.pdf']) {
			const response = await page.request.get(`/media/document-generation/${filename}`);
			expect(response.ok()).toBe(true);
			expect((await response.body()).length).toBeGreaterThan(1000);
		}
		for (const filename of ['revised-report.docx', 'revised-report.pdf', 'revised-deck.pptx', 'revised-deck.pdf']) {
			const response = await page.request.get(`/media/document-reference/${filename}`);
			expect(response.ok()).toBe(true);
			expect((await response.body()).length).toBeGreaterThan(1000);
		}
		await page.getByRole('main').locator('video').nth(0).scrollIntoViewIfNeeded();
		await page.screenshot({ path: test.info().outputPath(`word-guide-${width}.png`) });
		await page.getByRole('main').locator('video').nth(1).scrollIntoViewIfNeeded();
		await page.screenshot({ path: test.info().outputPath(`slides-guide-${width}.png`) });
		await page.getByRole('main').getByRole('link', { name: 'Open and edit documents', exact: true }).click();
		await expect(page).toHaveURL(/\/docs\/working-with-files\/$/);
		await expect(page.getByRole('main')).not.toContainText('OPAL');
		await expect(page.getByRole('main').locator('video')).toHaveCount(1);
		await page.screenshot({ path: test.info().outputPath(`file-viewer-guide-${width}.png`) });
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
