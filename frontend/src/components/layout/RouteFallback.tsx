/**
 * Suspense fallback while a page's code loads: a thin inline bar at the top of the page area.
 * Never a full-screen overlay; pages draw their own inline skeletons while their data loads.
 */
export function RouteFallback() {
  return (
    <div className="relative h-0.5 w-full overflow-hidden" role="progressbar" aria-label="Loading page" aria-busy="true">
      <div className="absolute inset-y-0 w-2/5 bg-brand-gradient animate-indeterminate" />
    </div>
  );
}
