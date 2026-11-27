import { useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '@/lib/cn';
import { useIsPhone } from '@/lib/hooks';
import { Button, IconButton } from './Button';
import { useModal } from './useModal';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
}

const DIALOG_SIZES = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl', xl: 'max-w-4xl' };

export function Dialog({ open, onClose, title, description, children, footer, size = 'md', className }: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();
  useModal(open, onClose, panelRef);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-end justify-center sm:items-center sm:p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[2px] animate-fade-in" onClick={onClose} aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        data-modal-panel
        className={cn(
          'relative flex max-h-[92vh] w-full flex-col overflow-hidden border border-ink-800 bg-ink-900 shadow-raised',
          'rounded-t-2xl animate-slide-up sm:rounded-2xl sm:animate-scale-in',
          DIALOG_SIZES[size],
          className,
        )}
      >
        <header className="flex items-start gap-3 border-b border-ink-800 px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-[15px] font-semibold text-ice-100">
              {title}
            </h2>
            {description && (
              <p id={descId} className="mt-1 text-[13px] text-muted">
                {description}
              </p>
            )}
          </div>
          <IconButton label="Close" icon={<X className="h-4 w-4" />} size="sm" onClick={onClose} tooltip={false} />
        </header>
        {children && <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>}
        {footer && (
          <footer className="pb-safe flex flex-wrap items-center justify-end gap-2 border-t border-ink-800 bg-ink-900 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}

export interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  confirmLabel?: string;
  tone?: 'primary' | 'danger';
  loading?: boolean;
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  children,
  confirmLabel = 'Confirm',
  tone = 'primary',
  loading,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      description={description}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={tone === 'danger' ? 'danger' : 'primary'} onClick={onConfirm} loading={loading} data-autofocus>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  /** Width on desktop (ignored on phones, where the drawer is a bottom sheet). */
  width?: string;
  /** Desktop side. Phones always use a bottom sheet. */
  side?: 'right' | 'left';
  headerExtra?: ReactNode;
}

/** Side sheet on desktop, bottom sheet on phones. Modal (focus-trapped). */
export function Drawer({ open, onClose, title, children, footer, width = '28rem', side = 'right', headerExtra }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const phone = useIsPhone();
  useModal(open, onClose, panelRef);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[70]">
      <div className="absolute inset-0 bg-black/50 animate-fade-in" onClick={onClose} aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-modal-panel
        style={phone ? undefined : { width }}
        className={cn(
          'absolute flex flex-col border-ink-800 bg-ink-900 shadow-raised',
          phone
            ? 'inset-x-0 bottom-0 max-h-[88vh] rounded-t-2xl border-t animate-slide-up'
            : cn('inset-y-0 max-w-[100vw] animate-slide-in-right', side === 'right' ? 'right-0 border-l' : 'left-0 border-r'),
        )}
      >
        {phone && <div className="mx-auto mt-2 h-1 w-10 shrink-0 rounded-full bg-ink-700" aria-hidden />}
        <header className="flex items-center gap-2 border-b border-ink-800 px-4 py-3">
          <h2 id={titleId} className="min-w-0 flex-1 truncate text-[14px] font-semibold">
            {title}
          </h2>
          {headerExtra}
          <IconButton label="Close" icon={<X className="h-4 w-4" />} size="sm" onClick={onClose} tooltip={false} />
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer && <footer className="pb-safe border-t border-ink-800 px-4 py-3">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
