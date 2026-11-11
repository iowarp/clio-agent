import { expect, test } from '@playwright/test';

test('workflow examples open their images from the caption and return focus', async ({ page }) => {
  const examples = [
    { route: '/docs/injections/', image: 'Expanded Wait for all tasks showing one collected result and the child agent\'s calculation', caption: 'Open the returned task result to read the check.' },
    { route: '/docs/injections/', image: 'Scheduled wake expanded above the resulting saved report', caption: 'Scheduled wake carries the saved instruction into the conversation.' },
    { route: '/docs/long-running-work/', image: 'Two Loop wake entries followed by separate saved monitoring reports', caption: 'Each Loop wake carries the monitoring instruction into another check.' },
    { route: '/docs/evidence/', image: 'Artifacts panel listing the bike example\'s source table, derived tables, findings report and dashboard', caption: 'Open a source or output from Artifacts' },
    { route: '/docs/sessions/', image: 'Marked messages list opened from the bookmark above the conversation rail', caption: 'Each entry keeps its title and a short excerpt' },
  ];
  for (const example of examples) {
    await page.goto(example.route);
    const figure = page.locator('figure').filter({ has: page.getByRole('img', { name: example.image, exact: true }) });
    const enlarge = figure.locator('figcaption').getByRole('button', { name: 'Click to enlarge.', exact: true });
    await enlarge.scrollIntoViewIfNeeded();
    await expect(enlarge).toBeEnabled();
    await enlarge.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('img', { name: example.image, exact: true })).toBeVisible();
    await expect(dialog).toContainText(example.caption);
    await expect(dialog.getByRole('button', { name: 'Click to enlarge.', exact: true })).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(enlarge).toBeFocused();
  }
});
