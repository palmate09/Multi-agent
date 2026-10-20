import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { resolveTheme, setThemePreference, THEME_STORAGE_KEY } from '../theme';

function stubSystem(scheme: 'light' | 'dark') {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('light') ? scheme === 'light' : scheme === 'dark',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }))
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('resolveTheme', () => {
  it('returns an explicit choice as is', () => {
    expect(resolveTheme('light')).toBe('light');
    expect(resolveTheme('dark')).toBe('dark');
  });

  it('follows the operating system for "system"', () => {
    stubSystem('light');
    expect(resolveTheme('system')).toBe('light');
    stubSystem('dark');
    expect(resolveTheme('system')).toBe('dark');
  });

  it('defaults to dark where the browser cannot say', () => {
    expect(resolveTheme('system')).toBe('dark');
  });
});

describe('setThemePreference', () => {
  it('sets data-theme on <html> and remembers an explicit choice', () => {
    setThemePreference('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
  });

  it('forgets the stored choice for "system" and resolves from the OS', () => {
    stubSystem('light');
    setThemePreference('dark');
    setThemePreference('system');
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('still applies the theme when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(() => setThemePreference('light')).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe('light');
    vi.restoreAllMocks();
  });
});
