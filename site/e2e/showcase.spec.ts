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
		const documents = showcase.getByRole('tab', { name: 'Documents', exact: true });
		await expect(documents).toHaveAttribute('aria-selected', 'true');
		await documents.focus();
		await page.keyboard.press('ArrowRight');
		await expect(showcase.getByRole('tab', { name: 'Evidence', exact: true })).toHaveAttribute('aria-selected', 'true');
		await expect(showcase.getByRole('heading', { name: 'Follow the files and steps behind a result.' })).toBeVisible();
		await showcase.getByRole('tab', { name: 'Figures', exact: true }).click();
		const enlarge = showcase.getByRole('button', { name: /^View larger:/ });
		await enlarge.click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toBeVisible();
		await expect(dialog).toContainText('not confirmed treatment doses');
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
		await page.locator('#delegate summary').focus();
		await page.keyboard.press('Enter');
		await expect(page.locator('#delegate')).toHaveAttribute('open', '');
		await expect(page.locator('#ask')).not.toHaveAttribute('open', '');
		await expect(page.locator('#delegate').getByRole('img')).toBeVisible();
		await documents.click();
		await showcase.getByRole('link', { name: 'Open the worked example' }).click();
		await expect(page).toHaveURL(/#review-a-complete-opal-1-briefing$/);
		await expect(page.getByRole('heading', { name: 'Review a complete OPAL 1 briefing' })).toBeInViewport();
	}
});

test('docs reveal the current guide group and distinguish account sign-in from workspace datasets', async ({ page }) => {
	await page.goto('/docs/');
	const filesGroup = page.locator('#starlight__sidebar details').filter({ has: page.getByRole('link', { name: 'Connect sources', exact: true, includeHidden: true }) }).last();
	await expect(filesGroup).not.toHaveAttribute('open', '');
	await page.getByRole('navigation', { name: 'Choose a CLIO guide' }).getByRole('link', { name: /Bring your data/ }).click();
	await expect(page).toHaveURL(/\/docs\/files\//);
	await expect(filesGroup).toHaveAttribute('open', '');
	await filesGroup.getByRole('link', { name: 'Connect sources', exact: true }).click();
	await expect(page.getByRole('main')).toContainText('Signing in does not attach a dataset');
	await expect(page.getByRole('main')).toContainText('Files → Sources');
	const image = page.getByRole('img', { name: /^Data sources settings lists/ });
	await expect(image).toBeVisible();
	expect((await page.request.get((await image.getAttribute('src'))!)).ok()).toBe(true);
});
