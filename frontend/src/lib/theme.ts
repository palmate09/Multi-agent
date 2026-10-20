import { useSyncExternalStore } from 'react';

/**
 * Theme preference: what the person chose. `system` follows the OS.
 * The resolved theme — what is actually painted — is always light or dark and
 * lives on <html data-theme="…">, which is what styles/tokens.css keys off.
 *
 * This is a tiny external store rather than React state so that the topbar's
 * switch and the sign-in page's switch stay in step, and so that the
 * preference can be applied before React has mounted anything.
 *
 * index.html runs a few lines of the same logic before first paint. That copy
 * exists only to avoid a flash of the wrong theme; this module is the one that
 * owns the behaviour afterwards.
 */
export type ThemePreference = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'mat-theme';

const listeners = new Set<() => void>();

function readStored(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    // Storage can be blocked (private mode, strict cookie settings). The
    // theme still works for this page load; it just is not remembered.
    return 'system';
  }
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === 'system' ? systemTheme() : preference;
}

let preference: ThemePreference = typeof window === 'undefined' ? 'system' : readStored();

function apply() {
  const root = document.documentElement;
  root.dataset.theme = resolveTheme(preference);

  // Keep the browser's own chrome (mobile address bar) in step with the page.
  const canvas = getComputedStyle(root).getPropertyValue('--color-bg-canvas').trim();
  if (canvas) {
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', canvas);
  }
}

function notify() {
  listeners.forEach(listener => listener());
}

export function setThemePreference(next: ThemePreference) {
  preference = next;
  try {
    if (next === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    /* not remembered; see readStored */
  }
  apply();
  notify();
}

/** Call once, before React renders. */
export function initTheme() {
  apply();

  // Follow the OS while the preference is `system`.
  window.matchMedia?.('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (preference === 'system') {
      apply();
      notify();
    }
  });

  // Another tab changed it.
  window.addEventListener('storage', e => {
    if (e.key !== THEME_STORAGE_KEY) return;
    preference = readStored();
    apply();
    notify();
  });
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useThemePreference(): ThemePreference {
  return useSyncExternalStore(
    subscribe,
    () => preference,
    () => 'system'
  );
}
