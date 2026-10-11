import { expect, test } from '@playwright/test';

const guides = [
	{ link: 'Best of N guide', route: '/docs/best-of-n/', title: 'Compare and refine answers' },
	{ link: 'Dashboard guide', route: '/docs/dashboards/', title: 'Dashboards' },
	{ link: 'Exports and downloads guide', route: '/docs/exports/', title: 'Exports and downloads' },
];

for (const theme of ['light', 'dark']) {
	for (const width of [390, 1440]) {
		test(`workflow guides are reachable and readable at ${width}px in ${theme}`, async ({ page }) => {
			await page.addInitScript((value) => localStorage.setItem('starlight-theme', value), theme);
			await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
			await page.goto('/');
			const workflows = page.getByRole('region', { name: 'Compare, assemble, and share.' });
			await workflows.scrollIntoViewIfNeeded();
			await expect(workflows.getByText('Development preview', { exact: true })).toHaveCount(2);
			expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
			await workflows.screenshot({ path: test.info().outputPath(`workflows-${width}-${theme}.png`) });
			for (const guide of guides) {
				await workflows.getByRole('link', { name: guide.link, exact: true }).click();
				await expect(page).toHaveURL(new RegExp(`${guide.route}$`));
				await expect(page.getByRole('heading', { level: 1, name: guide.title })).toBeVisible();
				await expect(page.locator('.sl-markdown-content .starlight-aside')).toContainText('not included in beta 5.2');
				expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
				await page.screenshot({ path: test.info().outputPath(`${guide.route.split('/')[2]}-${width}-${theme}.png`) });
				if (width === 390) await page.getByRole('button', { name: 'Docs index', exact: true }).click();
				await expect(page.locator('#starlight__sidebar').getByRole('link', { name: guide.title, exact: true })).toBeVisible();
				await page.goto('/');
			}
		});
	}
}

test('dashboard example opens at full size and draft examples separate judge modes', async ({ page }) => {
	await page.goto('/docs/dashboards/');
	const capture = page.getByRole('button', { name: /^View larger:/ }).first();
	await capture.scrollIntoViewIfNeeded();
	await capture.click();
	const dialog = page.getByRole('dialog');
	await expect(dialog).toContainText('complete report overview');
	await expect(dialog.getByRole('img')).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(dialog).toBeHidden();
	await expect(capture).toBeFocused();
	await page.goto('/docs/best-of-n/');
	await page.getByRole('tab', { name: 'The model chooses', exact: true }).click();
	await expect(page.getByRole('tabpanel')).toContainText('does not establish factual correctness');
	await page.getByRole('tab', { name: 'You choose', exact: true }).click();
	await expect(page.getByRole('tabpanel')).toContainText('let me choose');
});

test('capture waits for its handlers before accepting the first click', async ({ page }) => {
	let releaseScripts!: () => void;
	const scriptsReady = new Promise<void>((resolve) => { releaseScripts = resolve; });
	await page.route('**/_astro/*.js', async (route) => {
		await scriptsReady;
		await route.continue();
	});
	try {
		await page.goto('/docs/dashboards/', { waitUntil: 'commit' });
		const capture = page.getByRole('button', { name: /^View larger:/ }).first();
		await capture.scrollIntoViewIfNeeded();
		await expect(capture).toBeDisabled();
		releaseScripts();
		await expect(capture).toBeEnabled();
		await capture.click();
		await expect(page.getByRole('dialog')).toContainText('complete report overview');
	} finally {
		releaseScripts();
	}
});

test('the beginner path puts files before dashboards and alternatives under Go further', async ({ page }) => {
	await page.goto('/docs/dashboards/');
	const sidebar = page.locator('#starlight__sidebar');
	const order = await sidebar.locator('a').evaluateAll((links) => links.map((link) => link.getAttribute('href')));
	const basic = ['/docs/sessions/', '/docs/working-with-files/', '/docs/dashboards/', '/docs/exports/', '/docs/evidence/', '/docs/memory/'];
	for (let i = 1; i < basic.length; i++) expect(order.indexOf(basic[i])).toBeGreaterThan(order.indexOf(basic[i - 1]));
	const advanced = sidebar.locator('details').filter({ has: page.locator('summary', { hasText: 'Go further' }) });
	await expect(advanced.getByRole('link', { name: 'Compare and refine answers', exact: true })).toBeVisible();
});

test('task guides include playable short recordings, captions, and actual output downloads', async ({ page }) => {
	for (const route of ['/docs/best-of-n/', '/docs/dashboards/', '/docs/exports/']) {
		await page.goto(route);
		const video = page.locator('.sl-markdown-content video');
		await expect(video).toHaveCount(route.includes('best-of-n') ? 2 : 1);
		for (const recording of await video.all()) {
			for (const selector of ['source', 'track']) {
				const url = await recording.locator(selector).getAttribute('src');
				expect((await page.request.get(url!)).ok()).toBe(true);
			}
			const poster = await recording.getAttribute('poster');
			expect((await page.request.get(poster!)).ok()).toBe(true);
			await recording.evaluate((element: HTMLVideoElement) => element.load());
			await expect.poll(() => recording.evaluate((element: HTMLVideoElement) => element.duration)).toBeGreaterThan(10);
			expect(await recording.evaluate((element: HTMLVideoElement) => element.duration)).toBeLessThan(45);
		}
	}
	for (const url of ['/examples/bike-dashboard.png', '/examples/bike-dashboard.html', '/datasets/bike-stations.csv']) {
		expect((await page.request.get(url)).ok()).toBe(true);
	}
});

test('memory and evidence are separate guides and existing bookmarks redirect', async ({ page }) => {
	await page.goto('/docs/memory-and-evidence/');
	await expect(page).toHaveURL(/\/docs\/evidence\/$/);
	await expect(page.getByRole('heading', { level: 1, name: 'Evidence and lineage' })).toBeVisible();
	const sidebar = page.locator('#starlight__sidebar');
	await sidebar.getByRole('link', { name: 'Memory and recall', exact: true }).click();
	await expect(page.getByRole('heading', { level: 1, name: 'Memory and recall' })).toBeVisible();
	await expect(sidebar.getByRole('link', { name: 'Evidence and lineage', exact: true })).toBeVisible();
	const image = page.getByRole('button', { name: /^View larger:/ });
	await image.scrollIntoViewIfNeeded();
	await image.click();
	await expect(page.getByRole('dialog')).toContainText('first of four matches');
	await page.keyboard.press('Escape');
	await page.getByText('Inspect an actual returned overview', { exact: true }).click();
	const resultLink = page.getByRole('link', { name: 'recorded tool result', exact: true });
	const result = await page.request.get((await resultLink.getAttribute('href'))!);
	expect(result.ok()).toBe(true);
	const overview = await result.json();
	expect(overview.summary.metadata.source).toBe('gact_visible_transcript_summary');
	expect(overview.summary.recent_excerpts).toHaveLength(3);
	await page.goto('/docs/memory-and-evidence/#follow-artifact-lineage');
	await expect(page).toHaveURL(/\/docs\/evidence\/#follow-artifact-lineage$/);
	await expect(page.getByRole('heading', { name: 'Follow artifact lineage', exact: true })).toBeVisible();
	await page.goto('/docs/memory-and-evidence/#session-memory');
	await expect(page).toHaveURL(/\/docs\/memory\/#find-an-earlier-conversation$/);
});
