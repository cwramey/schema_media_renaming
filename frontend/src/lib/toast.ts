/** Module-level toast store, usable from React and from non-React code (query callbacks, WS). */
import { useSyncExternalStore } from 'react';

export type ToastTone = 'info' | 'success' | 'warning' | 'error';

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastItem {
  id: number;
  tone: ToastTone;
  title: string;
  description?: string;
  action?: ToastAction;
  /** ms; 0 keeps it until dismissed */
  duration: number;
}

type ToastInput = Omit<ToastItem, 'id' | 'tone' | 'duration'> & { duration?: number };

let items: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const timers = new Map<number, ReturnType<typeof setTimeout>>();

function emit() {
  listeners.forEach((l) => l());
}

export function dismissToast(id: number) {
  const t = timers.get(id);
  if (t) clearTimeout(t);
  timers.delete(id);
  items = items.filter((i) => i.id !== id);
  emit();
}

function push(tone: ToastTone, input: ToastInput): number {
  const id = nextId++;
  const duration = input.duration ?? (tone === 'error' ? 8000 : 4500);
  // De-duplicate identical messages that arrive in a burst.
  const dup = items.find((i) => i.title === input.title && i.description === input.description && i.tone === tone);
  if (dup) dismissToast(dup.id);
  items = [...items, { ...input, id, tone, duration }].slice(-5);
  if (duration > 0) timers.set(id, setTimeout(() => dismissToast(id), duration));
  emit();
  return id;
}

export const toast = {
  info: (title: string, opts: Omit<ToastInput, 'title'> = {}) => push('info', { title, ...opts }),
  success: (title: string, opts: Omit<ToastInput, 'title'> = {}) => push('success', { title, ...opts }),
  warning: (title: string, opts: Omit<ToastInput, 'title'> = {}) => push('warning', { title, ...opts }),
  error: (title: string, opts: Omit<ToastInput, 'title'> = {}) => push('error', { title, ...opts }),
  dismiss: dismissToast,
};

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => items,
    () => items,
  );
}
