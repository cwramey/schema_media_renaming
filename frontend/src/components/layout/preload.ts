/**
 * Lazily loaded pages. `preloadRoutes()` warms their chunks while the browser is idle so a
 * route change renders straight away instead of waiting on a Suspense fallback.
 */
export const loadWorkspace = () => import('@/features/jobs/WorkspacePage');
export const loadSettings = () => import('@/features/settings/SettingsPage');
export const loadFormatBuilder = () => import('@/features/formats/FormatBuilderPage');

const LOADERS = [loadWorkspace, loadSettings, loadFormatBuilder];

export function preloadRoutes(): () => void {
  const run = () => LOADERS.forEach((load) => void load().catch(() => undefined));
  if (typeof window.requestIdleCallback === 'function') {
    const id = window.requestIdleCallback(run, { timeout: 3000 });
    return () => window.cancelIdleCallback(id);
  }
  const t = setTimeout(run, 1200);
  return () => clearTimeout(t);
}
