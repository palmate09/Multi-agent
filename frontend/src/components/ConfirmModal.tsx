import { useEffect } from "react";

interface Props {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Destructive-action gate: backdrop click and Escape both cancel. */
export function ConfirmModal({ title, body, confirmLabel, busy, onConfirm, onCancel }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="modal-backdrop"
      onClick={onCancel}
      role="presentation"
    >
      <div
        className="card modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>{title}</h2>
        <div>{body}</div>
        <div className="modal-actions">
          <button className="ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button className="danger" disabled={busy} onClick={onConfirm}>
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
