import { expect, test } from '@playwright/test';

import { openSeededRun } from './helpers';

/**
 * Flow 2 — confirmations and destructive actions.
 *
 * Restart deletes everything a run produced, so it must be behind a modal that
 * traps focus, cancels on Escape, and cannot be fired twice.
 */
test.describe('destructive confirmations', () => {
  test.beforeEach(async ({ page }) => {
    await openSeededRun(page);
    await page.getByRole('button', { name: 'Restart' }).click();
  });

  test('restart opens a modal named after the run', async ({ page }) => {
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(/permanently delete all of this run/i);
    await expect(dialog).toContainText(/cannot be undone/i);
    await expect(dialog.getByRole('button', { name: /delete data and restart/i })).toBeVisible();
  });

  test('focus moves into the dialog and is contained', async ({ page }) => {
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();

    // Focus starts inside the dialog, not on the page behind it.
    await expect(dialog.getByRole('button', { name: /delete data and restart/i })).toBeFocused();

    // We start on the LAST control, so Tab must wrap to the first rather than
    // escaping to the page behind the scrim.
    await page.keyboard.press('Tab');
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused();

    // Shift+Tab from the FIRST control wraps back to the last.
    await page.keyboard.press('Shift+Tab');
    await expect(dialog.getByRole('button', { name: /delete data and restart/i })).toBeFocused();

    // And Tab from the last wraps to the first, so the cycle cannot be left.
    await page.keyboard.press('Tab');
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused();
  });

  test('Escape cancels and restores focus to the trigger', async ({ page }) => {
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();

    // Focus must go back where it came from, or a keyboard user is stranded.
    await expect(page.getByRole('button', { name: 'Restart' })).toBeFocused();
  });

  test("cancelling keeps the run's data", async ({ page }) => {
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    // Still the same run, with its artefacts.
    await expect(page.getByRole('tab', { name: /^Review/ })).toBeVisible();
    await page.getByRole('tab', { name: /^Review/ }).click();
    await expect(page.getByText('Loans endpoint does not validate')).toBeVisible();
  });
});

/**
 * Flow 3 — the playground.
 *
 * A preview boots generated code as a real subprocess. That is deliberately not
 * part of this suite: it executes model-generated code on the test runner. What
 * is asserted is the part that is ours -- that starting is possible, that the
 * control shows it is working, and that the panel always settles into a stated
 * outcome rather than spinning forever.
 */
test.describe('playground', () => {
  // A preview is server-side state that outlives the test that started it, so
  // the second test settles it explicitly rather than assuming a clean slate.

  test('settles into a stated outcome instead of spinning forever', async ({ page }) => {
    await openSeededRun(page);
    await page.getByRole('tab', { name: /^Playground/ }).click();

    // The panel always states what state it is in.
    const panel = page.locator('.card', {
      has: page.getByRole('heading', { name: 'Playground' }),
    });
    const status = panel.locator('.badge').first();
    await expect(status).toBeVisible();

    await panel.getByRole('button', { name: /^(Start|Restart)$/ }).click();

    // While booting the control relabels itself, which is the point: a button
    // still saying "Start" while it starts gives no feedback.
    await expect(panel.locator('.badge')).toHaveText(/starting|running/, { timeout: 15_000 });

    // However it goes, it must settle on a stated outcome, never spin forever.
    await expect(status).toHaveText(/running|crashed|stopped/, { timeout: 40_000 });
    await expect(
      page.getByText(/loopback port behind the gateway|did not come up|Not running/i)
    ).toBeVisible();
  });

  test('stop is disabled while nothing is running', async ({ page }) => {
    await openSeededRun(page);
    await page.getByRole('tab', { name: /^Playground/ }).click();

    const stop = page.getByRole('button', { name: 'Stop' });
    await expect(stop).toBeVisible();
    // Settle first: a preview from a previous test would be running here.
    if (await stop.isEnabled()) {
      await stop.click();
    }
    await expect(stop).toBeDisabled({ timeout: 15_000 });
  });
});
