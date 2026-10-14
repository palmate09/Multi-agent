import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

export type BadgeTone = 'good' | 'warn' | 'bad' | 'idle';

export interface BadgeProps {
  tone: BadgeTone;
  children: ReactNode;
  /** Animates the dot. Only for a state that is still changing. */
  pulse?: boolean;
  /** Extra context on hover. The tone is never conveyed by colour alone —
   *  `children` is always the status text itself. */
  title?: string;
}

/**
 * A run status, a preview state, or the signed-in user.
 *
 * The dot is decorative and empty, so it is hidden from assistive tech; the
 * status word next to it is what carries the meaning.
 */
export function Badge({ tone, children, pulse = false, title }: BadgeProps) {
  return (
    <span className={cn('badge', tone)} title={title}>
      <span className={cn('dot', pulse && 'pulse')} aria-hidden="true" />
      {children}
    </span>
  );
}
