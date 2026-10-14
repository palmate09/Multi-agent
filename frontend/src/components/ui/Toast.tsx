import { useEffect, useRef } from 'react';

import { cn } from '../../lib/cn';

export type ToastTone = 'info' | 'success' | 'error';

export interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

export interface ToastListProps {
  toasts: Toast[];
  onDismiss: (_id: number) => void;
  /** How long a toast stays up. Errors stay longer: they carry a reason. */
  timeoutMs?: number;
}

/**
 * Transient confirmations.
 *
 * The list is a live region so a message that appears after a click is read
 * out; without it, starting a run is completely silent for anyone not looking
 * at the screen. Errors use `role="alert"`, everything else `role="status"`.
 */
export function ToastList({ toasts, onDismiss, timeoutMs = 4000 }: ToastListProps) {
  return (
    <div className="toast-list">
      {toasts.map(t => (
        <ToastItem
          key={t.id}
          toast={t}
          onDismiss={onDismiss}
          timeoutMs={t.tone === 'error' ? timeoutMs * 2 : timeoutMs}
        />
      ))}
    </div>
  );
}

function ToastItem({
  toast,
  onDismiss,
  timeoutMs,
}: {
  toast: Toast;
  onDismiss: (_id: number) => void;
  timeoutMs: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), timeoutMs);
    return () => clearTimeout(timer);
  }, [toast.id, timeoutMs, onDismiss]);

  return (
    <div
      ref={ref}
      className={cn('toast', `toast-${toast.tone}`)}
      role={toast.tone === 'error' ? 'alert' : 'status'}
    >
      <span className="toast-msg">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        onClick={() => onDismiss(toast.id)}
        aria-label={`Dismiss: ${toast.message}`}
      >
        ✕
      </button>
    </div>
  );
}
