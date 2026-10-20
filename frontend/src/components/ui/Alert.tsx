import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';
import { Icon } from './Icon';
import type { IconName } from './Icon';

export type AlertTone = 'note' | 'error' | 'success' | 'warning';

export interface AlertProps {
  tone: AlertTone;
  children: ReactNode;
  className?: string;
}

const ICON: Record<AlertTone, IconName> = {
  note: 'info',
  error: 'alert',
  success: 'check-circle',
  warning: 'warning',
};

/**
 * An inline message about the thing above it.
 *
 * `error` is `role="alert"` (assertive): a failure the user has to react to.
 * The other tones are `role="status"` (polite): information that can wait for
 * a gap in whatever is being read out. The icon repeats the tone for people
 * who cannot tell the colours apart; it is decorative to assistive tech.
 */
export function Alert({ tone, children, className }: AlertProps) {
  return (
    <div className={cn('alert', tone, className)} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon name={ICON[tone]} className="alert-icon" />
      <div className="alert-body">{children}</div>
    </div>
  );
}
