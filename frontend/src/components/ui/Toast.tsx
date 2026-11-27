import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react';
import { cn } from '@/lib/cn';
import { dismissToast, useToasts, type ToastTone } from '@/lib/toast';

const ICON: Record<ToastTone, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
};

const TONE: Record<ToastTone, string> = {
  info: 'text-info',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-danger',
};

/** Fixed toast stack. Bottom-right on desktop; above the tab bar on phones. */
export function ToastViewport() {
  const toasts = useToasts();
  return (
    <div
      aria-live="polite"
      aria-relevant="additions"
      className="pointer-events-none fixed inset-x-3 bottom-20 z-[120] flex flex-col items-center gap-2 md:inset-x-auto md:bottom-4 md:right-4 md:items-end"
    >
      {toasts.map((t) => {
        const Icon = ICON[t.tone];
        return (
          <div
            key={t.id}
            role={t.tone === 'error' ? 'alert' : 'status'}
            className="pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border border-ink-700 bg-ink-850 px-3.5 py-3 shadow-raised animate-slide-up"
          >
            <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', TONE[t.tone])} aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-medium text-ice-100">{t.title}</p>
              {t.description && <p className="mt-0.5 break-words text-xs text-muted">{t.description}</p>}
              {t.action && (
                <button
                  type="button"
                  onClick={() => {
                    t.action?.onClick();
                    dismissToast(t.id);
                  }}
                  className="mt-2 text-xs font-semibold text-accent hover:underline"
                >
                  {t.action.label}
                </button>
              )}
            </div>
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => dismissToast(t.id)}
              className="-mr-1 rounded p-0.5 text-muted hover:text-ice-100"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
