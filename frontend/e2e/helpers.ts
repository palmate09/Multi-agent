import { expect } from '@playwright/test';
import type { Page } from '@playwright/test';

/**
 * Credentials come from the environment in playwright.config.ts. The password
 * is only ever a fixture value for a throwaway local server.
 */
export const E2E_PASSWORD = 'e2e-password';
export const SEEDED_RUN = 'verify_library_e2e';
/** The list shows the requirement, not the run id. The id only appears in the
 *  delete button's accessible name. */
export const SEEDED_REQUIREMENT = 'lends books';

/** Sign in through the real form, as a user would. */
export async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Username').fill('admin');
  await page.getByLabel('Password').fill(E2E_PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('banner')).toBeVisible();
}

/** Open the seeded, already-finished run from the sidebar. */
export async function openSeededRun(page: Page) {
  await signIn(page);
  const row = page.locator('.run-item').filter({ hasText: SEEDED_REQUIREMENT }).first();
  await expect(row).toBeVisible();
  await row.locator('.run-open').click();
  await expect(page.getByRole('heading', { name: /Run / })).toBeVisible();
}
