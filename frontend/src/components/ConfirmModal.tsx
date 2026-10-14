import type { ReactNode } from 'react';

import { Modal } from './ui/Modal';

interface Props {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Destructive-action gate. A thin wrapper over the shared Modal so that every
 * confirmation in the app gets the same focus handling, the same Escape and
 * backdrop behaviour, and the same busy state that blocks double-firing.
 */
export function ConfirmModal({ title, body, confirmLabel, busy, onConfirm, onCancel }: Props) {
  return (
    <Modal
      open
      title={title}
      confirmLabel={confirmLabel}
      busy={busy}
      confirmVariant="danger"
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
      {body}
    </Modal>
  );
}
