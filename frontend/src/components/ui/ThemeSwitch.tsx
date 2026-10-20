import { setThemePreference, useThemePreference } from '../../lib/theme';
import type { ThemePreference } from '../../lib/theme';
import { Icon } from './Icon';
import type { IconName } from './Icon';

const OPTIONS: ReadonlyArray<{ value: ThemePreference; label: string; icon: IconName }> = [
  { value: 'system', label: 'System', icon: 'monitor' },
  { value: 'light', label: 'Light', icon: 'sun' },
  { value: 'dark', label: 'Dark', icon: 'moon' },
];

/**
 * Light / dark / follow-the-system.
 *
 * A group of toggle buttons rather than a single icon that cycles: a control
 * that changes the screen should show every choice and which one is current,
 * and `aria-pressed` says so to a screen reader. The labels are visually
 * hidden at small sizes but always in the accessible name.
 */
export function ThemeSwitch() {
  const current = useThemePreference();

  return (
    <div className="theme-switch" role="group" aria-label="Theme">
      {OPTIONS.map(o => (
        <button
          key={o.value}
          type="button"
          className="theme-option"
          aria-pressed={current === o.value}
          aria-label={o.label}
          title={`${o.label} theme`}
          onClick={() => setThemePreference(o.value)}
        >
          <Icon name={o.icon} size={15} />
        </button>
      ))}
    </div>
  );
}
