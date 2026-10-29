import { expect, test } from '@playwright/test';

const guides = [
	{ link: 'Best of N guide', route: '/docs/best-of-n/', title: 'Compare alternative answers' },
	{ link: 'Dashboard guide', route: '/docs/dashboards/', title: 'Explore charts and dashboards' },
	{ link: 'Exports and downloads guide', route: '/docs/exports/', title: 'Save and share your work' },
];

for (const theme of ['light', 'dark']) {
	for (const width of [390, 1440]) {
		test(`workflow guides are reachable and readable at ${width}px in ${theme}`, async ({ page }) => {
			await page.addInitScript((value) => localStorage.setItem('starlight-theme', value), theme);
			await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
			await page.goto('/');
			const workflows = page.getByRole('region', { name: 'Study, delegate, compare, and share.' });
			await workflows.scrollIntoViewIfNeeded();
			await expect(workflows.getByText('Development preview', { exact: true })).toHaveCount(5);
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

test('the docs group data, results and technical reference around reader tasks', async ({ page }) => {
	await page.goto('/docs/dashboards/');
	const sidebar = page.locator('#starlight__sidebar');
	const groups = await sidebar.locator('summary').allTextContents();
	expect(groups.map((label) => label.trim())).toEqual([
		'Get started', 'Bring your data', 'Work with CLIO', 'Tasks and coordination', 'Review and share results',
		'Find and revisit work', 'Worked examples', 'Customize CLIO', 'Build with the SDK', 'Help and privacy',
		'Technical reference',
	]);
	const results = sidebar.locator('details').filter({ has: page.locator('summary', { hasText: 'Review and share results' }) });
	await expect(results.getByRole('link', { name: 'Compare alternative answers', exact: true })).toBeVisible();
	await expect(results.getByRole('link', { name: 'Save and share your work', exact: true })).toBeVisible();
	const reference = sidebar.locator('details').filter({ has: page.locator('summary', { hasText: 'Technical reference' }) });
	await expect(reference).not.toHaveAttribute('open');
	await reference.locator('summary').click();
	await expect(reference.getByRole('link', { name: 'Server and terminal setup', exact: true })).toBeVisible();
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
	await expect(page.getByRole('heading', { level: 1, name: 'Check answers and sources' })).toBeVisible();
	const sidebar = page.locator('#starlight__sidebar');
	await sidebar.locator('summary').filter({ hasText: 'Find and revisit work' }).click();
	await sidebar.getByRole('link', { name: 'What CLIO remembers', exact: true }).click();
	await expect(page.getByRole('heading', { level: 1, name: 'What CLIO remembers' })).toBeVisible();
	await sidebar.locator('summary').filter({ hasText: 'Review and share results' }).click();
	await expect(sidebar.getByRole('link', { name: 'Check answers and sources', exact: true })).toBeVisible();
	const image = page.getByRole('button', { name: /^View larger:/ });
	await image.scrollIntoViewIfNeeded();
	await image.click();
	await expect(page.getByRole('dialog')).toContainText('Open returns to the matching source message.');
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
