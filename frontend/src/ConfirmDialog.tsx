import React, { useEffect, useRef } from 'react';

// A shadcn-style AlertDialog: the confirmation step in front of an action that
// cannot be taken back or that costs real money.
//
// It is a component and not an inline `window.confirm` for two reasons. The
// native dialog cannot name what is about to happen in more than one line of
// unstyled text -- and every action that reaches this component needs to say
// which run, which symbol, how many dollars. And an inline "are you sure?"
// swapped into a table cell resizes the column it lives in, so the row under
// the cursor moves while the user is deciding.
//
// The a11y contract of a modal is not optional furniture here: it is what stops
// the confirmation from being skippable. Everything below is that contract.

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** What will happen, in the user's terms. Name the specific object. */
  description: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** `destructive` paints the confirm button solid red. */
  tone?: 'default' | 'destructive';
  /** In flight: both buttons lock so the action cannot be fired twice. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const ConfirmDialog = ({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone = 'default',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) => {
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Whatever had focus when the dialog opened, so closing it does not dump the
  // user back at the top of the document.
  const returnTo = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    returnTo.current = document.activeElement;
    // Cancel, not Confirm. The safe option is the one that must be reachable by
    // pressing Enter immediately, and the destructive one is the one that has
    // to be aimed at.
    cancelRef.current?.focus();
    const previous = returnTo.current as HTMLElement | null;
    return () => {
      if (previous && typeof previous.focus === 'function') previous.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        if (!busy) onCancel();
        return;
      }
      if (e.key !== 'Tab') return;
      // A two-button trap. Written out rather than pulled from a library
      // because the alternative is not "no trap" -- it is Tab walking the user
      // out of the dialog and into the table behind it, where the row they are
      // deciding about is still clickable.
      const focusables = [cancelRef.current, confirmRef.current].filter(
        Boolean,
      ) as HTMLButtonElement[];
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      } else if (!panelRef.current?.contains(active)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      className="dialog-overlay"
      // Clicking the backdrop cancels, which is the safe direction. Guarded on
      // the target so a click that started inside the panel and ended on the
      // overlay does not dismiss it.
      onClick={e => {
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={panelRef}
        className="dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-desc"
      >
        <h2 className="dialog-title" id="confirm-dialog-title">
          {title}
        </h2>
        <div className="dialog-desc" id="confirm-dialog-desc">
          {description}
        </div>
        <div className="dialog-actions">
          <button
            ref={cancelRef}
            type="button"
            className="btn btn-outline"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`btn ${tone === 'destructive' ? 'btn-destructive' : 'btn-primary'}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmDialog;
