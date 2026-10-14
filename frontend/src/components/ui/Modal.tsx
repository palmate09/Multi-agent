/* eslint-disable jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions */
import { useCallback, useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';
import { Button } from './Button';

export interface ModalProps {
  open: boolean;
  /** The heading, rendered visibly and used as the dialog's accessible name. */
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  /** Shows a busy state on the confirm button and blocks dismissal, so a
   *  destructive action cannot be fired twice. */
  busy?: boolean;
  /** `danger` for anything irreversible. */
  confirmVariant?: 'danger' | 'primary';
  cancelLabel?: string;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A modal dialog that behaves like one.
 *
 * Three things a dialog must do and this one therefore does: move focus in on
 * open, keep Tab inside while it is open, and put focus back where it was on
 * close. Without the first two, Tab from the trigger behind the dialog moves
 * through the obscured page — the exact failure mode a modal exists to
 * prevent. Escape and a backdrop click both cancel, and neither fires while
 * the action is in flight.
 */
export function Modal({
  open,
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
  confirmVariant = 'primary',
  cancelLabel = 'Cancel',
}: ModalProps) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const restoreTo = useRef<HTMLElement | null>(null);
  // The callbacks are almost always inline arrows, so depending on them
  // directly would re-run the focus effect on every render of the parent --
  // which steals focus back to the confirm button mid-interaction. Holding
  // them in refs keeps this effect tied to `open` and nothing else.
  const onCancelRef = useRef(onCancel);
  // Updated in an effect rather than during render: assigning `ref.current`
  // while rendering is not allowed, and doing it here keeps `handleKey` stable
  // without re-running the focus effect on every render of the parent.
  useEffect(() => {
    onCancelRef.current = onCancel;
  });

  const handleKey = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      onCancelRef.current();
      return;
    }
    if (e.key !== 'Tab') return;

    const root = dialogRef.current;
    if (!root) return;
    const items = [...root.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;

    // Wrap at both ends, so Tab never escapes to the page behind.
    if (e.shiftKey && (active === first || !root.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }, []);

  useEffect(() => {
    if (!open) return;

    restoreTo.current = document.activeElement as HTMLElement | null;
    // A dialog must take focus on open. The confirm button is the right
    // default for a destructive gate.
    const root = dialogRef.current;
    const target = root?.querySelector<HTMLElement>('.modal-actions button:last-of-type');
    (target ?? root)?.focus();

    document.addEventListener('keydown', handleKey);
    // Stop the page behind scrolling under the scrim.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    return () => {
      document.removeEventListener('keydown', handleKey);
      document.body.style.overflow = previousOverflow;
      restoreTo.current?.focus();
    };
  }, [open, handleKey]);

  if (!open) return null;

  return (
    <div
      className="modal-backdrop"
      onClick={busy ? undefined : onCancel}
      onKeyDown={e => {
        if (e.key === 'Escape' && !busy) onCancel();
      }}
      role="presentation"
    >
      <div
        ref={dialogRef}
        className="card modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        // A click inside must not reach the backdrop's cancel handler.
        onClick={e => e.stopPropagation()}
      >
        <h2 id={titleId}>{title}</h2>
        <div>{children}</div>
        <div className={cn('modal-actions')}>
          <Button variant="ghost" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button
            variant={confirmVariant}
            loading={busy}
            loadingLabel="Working…"
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
