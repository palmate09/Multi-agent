import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

export interface StatProps {
  /** The number, or "—" when there is nothing to report. */
  value: ReactNode;
  /** What the number means, in sentence case — it sits under the value at
   *  10px uppercase, so it has to be short enough to read at a glance. */
  label: string;
}

/** One number with its label. Never used on its own: stats travel in a grid,
 *  because a single tile reads as an orphan. */
export function Stat({ value, label }: StatProps) {
  return (
    <div className="stat">
      <div className="v">{value}</div>
      <div className="k">{label}</div>
    </div>
  );
}

/** A responsive grid of stats: as many as fit, at least 110px each. */
export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('stats', className)}>{children}</div>;
}
