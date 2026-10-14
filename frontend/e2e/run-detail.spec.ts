import { expect, test } from '@playwright/test';

import { openSeededRun, SEEDED_REQUIREMENT, SEEDED_RUN, signIn } from './helpers';

/**
 * Flow 1 — sign in, then read a finished run's verdict and artefacts.
 *
 * Backed by the seeded run (see e2e/fixtures/seed_run.py), so it exercises the
 * whole run-detail console with no LLM and no backend change.
 */
test.describe('sign in and read a finished run', () => {
  test('rejects a bad password and keeps what was typed', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('Username').fill('admin');
    await page.getByLabel('Password').fill('wrong-password');
    await page.getByRole('button', { name: 'Sign in' }).click();

    // The failure is announced, not silent.
    const alert = page.getByRole('alert');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/invalid username or password/i);

    // Redundant entry (WCAG 2.2 SC 3.3.7): a retry must not mean retyping.
    await expect(page.getByLabel('Username')).toHaveValue('admin');
    await expect(page.getByLabel('Password')).toHaveValue('wrong-password');

    // Still signed out.
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  });

  test('signs in and shows the run list', async ({ page }) => {
    await signIn(page);
    await expect(page.getByRole('navigation', { name: 'Pipeline runs' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'New run' })).toBeVisible();
  });

  test("renders the seeded run's verdict and every artefact tab", async ({ page }) => {
    await openSeededRun(page);

    // The status badge says accepted — in text, not only by colour.
    await expect(page.getByText('accepted', { exact: true }).first()).toBeVisible();

    // Stats come from the seeded summary.json.
    await expect(page.locator('.stat', { hasText: 'Tests passed' })).toContainText('5');
    await expect(page.locator('.stat', { hasText: 'Wall (s)' })).toContainText('73');

    // Domain check: conclusive and complete.
    await expect(page.getByText(/all requirement terms present \(3\)/)).toBeVisible();

    const tab = (name: string) => page.getByRole('tab', { name: new RegExp(`^${name}`) });

    await tab('Stories').click();
    await expect(page.getByText('List books')).toBeVisible();
    await expect(page.getByText(/Clarifying question:/)).toBeVisible();

    await tab('Plan').click();
    await expect(page.getByRole('heading', { name: 'Approach' })).toBeVisible();
    await expect(page.getByText('Risks (1)')).toBeVisible();
    await expect(page.getByText(/Advisory: it guides the Designer/)).toBeVisible();

    await tab('OpenAPI').click();
    await expect(page.locator('pre.code')).toContainText('Library API');

    await tab('Code').click();
    await expect(page.getByRole('tab', { name: 'app.py' })).toBeVisible();
    await expect(page.locator('pre.code')).toContainText('class Book');

    await tab('Tests').click();
    await expect(page.getByRole('tab', { name: 'test_app.py' })).toBeVisible();
    await expect(page.locator('pre.code')).toContainText('def test_health');

    await tab('Test report').click();
    const reportPanel = page.getByRole('tabpanel');
    // The label is uppercased by CSS, so assert on the tile count and the
    // pytest output rather than on a case-sensitive label match.
    await expect(reportPanel.locator('.stat')).toHaveCount(2);
    await expect(reportPanel.locator('pre.code')).toContainText('5 passed in 0.42s');

    await tab('Review').click();
    await expect(page.getByText('Loans endpoint does not validate')).toBeVisible();
    // The tab label carries the comment count, so it is not colour-only.
    await expect(tab('Review')).toContainText('(2)');
  });

  test('tab strip is one tab stop and arrow keys move between tabs', async ({ page }) => {
    await openSeededRun(page);

    const pipeline = page.getByRole('tab', { name: /^Pipeline/ });
    await pipeline.focus();
    // Roving tabindex: the active tab is the only one in the tab order.
    await expect(pipeline).toHaveAttribute('tabindex', '0');
    await expect(page.getByRole('tab', { name: /^Stories/ })).toHaveAttribute('tabindex', '-1');

    await pipeline.press('ArrowRight');
    await expect(page.getByRole('tab', { name: /^Stories/ })).toBeFocused();
    await expect(page.getByText('List books')).toBeVisible();

    await page.getByRole('tab', { name: /^Stories/ }).press('End');
    await expect(page.getByRole('tab', { name: /^Playground/ })).toBeFocused();
  });

  test('a run with no timeline shows the empty state, not a blank panel', async ({ page }) => {
    await openSeededRun(page);
    await page.getByRole('tab', { name: /^Pipeline/ }).click();
    // Events are an in-memory buffer mirrored to the database index; a run
    // rehydrated from disk has none. The empty state must say so.
    await expect(page.getByText(/No pipeline activity yet/)).toBeVisible();
  });

  test('the seeded run survives a reload', async ({ page }) => {
    await openSeededRun(page);
    await page.reload();
    await expect(page.getByRole('navigation', { name: 'Pipeline runs' })).toBeVisible();
    await expect(page.getByText(SEEDED_REQUIREMENT)).toBeVisible();
    // The run id is only exposed through the delete button's accessible name.
    await expect(page.getByRole('button', { name: `Delete run ${SEEDED_RUN}` })).toBeVisible();
  });
});
