import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

export type AlertTone = 'note' | 'error';

export interface AlertProps {
  tone: AlertTone;
  children: ReactNode;
  className?: string;
}

/**
 * An inline message about the thing above it.
 *
 * `error` is `role="alert"` (assertive): a failure the user has to react to.
 * `note` is `role="status"` (polite): advisory information that can wait for
 * a gap in whatever is being read out.
 */
export function Alert({ tone, children, className }: AlertProps) {
  return (
    <div className={cn('alert', tone, className)} role={tone === 'error' ? 'alert' : 'status'}>
      {children}
    </div>
  );
}
