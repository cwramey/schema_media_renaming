/** Theme preference store: dark-first, light toggle, follows prefers-color-scheme when "system". */
import { useSyncExternalStore } from 'react';
import { readStorage, writeStorage } from './hooks';

export type ThemePref = 'system' | 'dark' | 'light';
export type Theme = 'dark' | 'light';

const KEY = 'schema.theme';
const listeners = new Set<() => void>();
let pref: ThemePref = readStorage<ThemePref>(KEY, 'system');

const systemTheme = (): Theme => (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');

export const resolveTheme = (p: ThemePref = pref): Theme => (p === 'system' ? systemTheme() : p);

function notify() {
  listeners.forEach((l) => l());
}

export function applyTheme() {
  const theme = resolveTheme();
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'dark' ? '#15181D' : '#F3F5F8');
}

export function setThemePref(next: ThemePref) {
  pref = next;
  writeStorage(KEY, next);
  applyTheme();
  notify();
}

export function initTheme() {
  applyTheme();
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (pref !== 'system') return;
    applyTheme();
    notify();
  });
}

export function useTheme() {
  const p = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pref,
    () => pref,
  );
  const theme = resolveTheme(p);
  return {
    pref: p,
    theme,
    setPref: setThemePref,
    toggle: () => setThemePref(theme === 'dark' ? 'light' : 'dark'),
  };
}
