import type { ButtonHTMLAttributes } from 'react';

import { cn } from '../../lib/cn';

export type ButtonVariant = 'default' | 'primary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual treatment. `danger` is only for irreversible actions. */
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows the loading label and blocks interaction, without changing layout. */
  loading?: boolean;
  /** What the button says while `loading`. Defaults to the children. */
  loadingLabel?: string;
}

/**
 * The one button in the app.
 *
 * `loading` implies disabled and sets `aria-busy`, so a screen reader hears
 * that the control is working rather than just going quiet. The label changes
 * to `loadingLabel` for the same reason: a button that says "Restart" while
 * restarting gives no feedback that anything happened.
 */
export function Button({
  variant = 'default',
  size = 'md',
  loading = false,
  loadingLabel,
  disabled,
  className,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn('btn', variant !== 'default' && variant, size === 'sm' && 'btn-sm', className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && loadingLabel ? loadingLabel : children}
    </button>
  );
}
