import { cn } from '../../lib/cn';

export interface SkeletonProps {
  /** Rendered shape. `text` is one line, `block` is a filled rectangle. */
  variant?: 'text' | 'block' | 'row';
  width?: string;
  height?: string;
  className?: string;
}

/**
 * A loading placeholder.
 *
 * Skeletons are `aria-hidden` and the container that holds them owns the
 * `aria-busy` state, so a screen reader hears "busy" once instead of a pile of
 * empty boxes. They also never animate under `prefers-reduced-motion` — the
 * shimmer is disabled by the global rule in styles.css.
 */
export function Skeleton({ variant = 'text', width, height, className }: SkeletonProps) {
  return (
    <div
      className={cn('skeleton', `skeleton-${variant}`, className)}
      style={{ width, height }}
      aria-hidden="true"
    />
  );
}

/** A labelled block of skeletons, for "this panel is loading". */
export function SkeletonGroup({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div className="skeleton-group" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} variant="row" width={i === rows - 1 ? '62%' : '100%'} />
      ))}
    </div>
  );
}
