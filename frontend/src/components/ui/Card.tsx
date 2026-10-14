import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

export interface CardProps {
  children: ReactNode;
  className?: string;
  /** Rendered as a real `<section>` with an accessible name, for a card that
   *  is a landmark in its own right. */
  label?: string;
}

/** A raised panel. Padding, border and radius come from `.card`. */
export function Card({ children, className, label }: CardProps) {
  if (label) {
    return (
      <section className={cn('card', className)} aria-label={label}>
        {children}
      </section>
    );
  }
  return <div className={cn('card', className)}>{children}</div>;
}

export interface EmptyStateProps {
  children: ReactNode;
  /** A next action, so an empty panel is never a dead end. */
  action?: ReactNode;
}

/** "Nothing here yet" — always with a way to put something there. */
export function EmptyState({ children, action }: EmptyStateProps) {
  return (
    <div className="empty">
      <p>{children}</p>
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}
