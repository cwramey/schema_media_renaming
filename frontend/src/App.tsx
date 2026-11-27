import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from 'react';
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation, useParams } from 'react-router-dom';
import { QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { createQueryClient } from '@/api/queryClient';
import { onUnauthorized } from '@/api/client';
import { qk } from '@/api/keys';
import { useAuthStatus } from '@/api/hooks';
import { Splash } from '@/components/brand/Mark';
import { ToastViewport } from '@/components/ui';
import { AppShell } from '@/components/layout/AppShell';
import { RouteFallback } from '@/components/layout/RouteFallback';
import { NotFoundPage } from '@/components/layout/NotFoundPage';
import { loadFormatBuilder, loadSettings, loadWorkspace } from '@/components/layout/preload';
import { ServerUnreachable } from '@/features/auth/ServerUnreachable';
import { LoginPage, SetupPage } from '@/features/auth/AuthPages';
import { HomePage } from '@/features/home/HomePage';
import { HistoryPage } from '@/features/history/HistoryPage';
import { LEGACY_REDIRECTS, settingsTabTarget } from '@/lib/routes';

const WorkspacePage = lazy(loadWorkspace);
const FormatBuilderPage = lazy(loadFormatBuilder);
const SettingsPage = lazy(loadSettings);

const queryClient = createQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Root />
      </BrowserRouter>
      <ToastViewport />
    </QueryClientProvider>
  );
}

function Root() {
  const qc = useQueryClient();
  const auth = useAuthStatus();
  const [splashGone, setSplashGone] = useState(false);
  const onGone = useCallback(() => setSplashGone(true), []);

  // Any 401 from a data route means the session ended: re-check auth status.
  useEffect(() => onUnauthorized(() => void qc.invalidateQueries({ queryKey: qk.authStatus })), [qc]);

  return (
    <>
      {!splashGone && <Splash done={!auth.isPending} onGone={onGone} />}
      {auth.isPending ? null : auth.isError && !auth.data ? (
        <ServerUnreachable error={auth.error} onRetry={() => void auth.refetch()} />
      ) : (
        <Routes>
          <Route path="/setup" element={<SetupPage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route element={<RequireAuth />}>
            <Route element={<AppShell />}>
              <Route index element={<HomePage />} />
              <Route path="history" element={<HistoryPage />} />
              <Route path="jobs/:id" element={<Lazy el={<WorkspacePage />} />} />
              <Route path="settings" element={<Lazy el={<SettingsPage />} />} />
              <Route path="settings/formula" element={<Lazy el={<FormatBuilderPage />} />} />
              <Route path="settings/:tab" element={<SettingsTabRedirect />} />
              {Object.entries(LEGACY_REDIRECTS).map(([from, to]) => (
                <Route key={from} path={from} element={<Navigate to={to} replace />} />
              ))}
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Routes>
      )}
    </>
  );
}

/** Lazily loaded page; the fallback is an inline bar (chunks are preloaded, see preload.ts). */
function Lazy({ el }: { el: ReactNode }) {
  return <Suspense fallback={<RouteFallback />}>{el}</Suspense>;
}

/** Old `/settings/<tab>` links land on the matching section of the one-page Settings. */
function SettingsTabRedirect() {
  const { tab } = useParams();
  return <Navigate to={settingsTabTarget(tab)} replace />;
}

function RequireAuth() {
  const { data } = useAuthStatus();
  const location = useLocation();
  if (!data) return null;
  if (data.setup_required) return <Navigate to="/setup" replace />;
  if (data.auth_enabled && !data.authenticated) {
    const next = location.pathname + location.search;
    return <Navigate to={next && next !== '/' ? `/login?next=${encodeURIComponent(next)}` : '/login'} replace />;
  }
  return <Outlet />;
}
