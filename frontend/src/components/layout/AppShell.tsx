import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { Check, FlaskConical, Search, ShieldAlert, X } from 'lucide-react';
import { useAuthStatus } from '@/api/hooks';
import { useLiveUpdates } from '@/api/ws';
import { cn } from '@/lib/cn';
import { historySearchLink } from '@/lib/routes';
import { Button, IconButton, controlBase } from '@/components/ui';
import { Mark, Wordtype } from '@/components/brand/Mark';
import { ImportDrawer } from '@/features/import/ImportDrawer';
import { NAV } from './nav';
import { preloadRoutes } from './preload';

function Brand() {
  return (
    <NavLink
      to="/"
      className="order-1 flex h-11 shrink-0 items-center gap-3 rounded-lg pr-1 md:order-none lg:justify-self-start"
      aria-label="Schema home"
    >
      <Mark size={36} />
      <Wordtype height={24} />
    </NavLink>
  );
}

function MainNav() {
  return (
    <nav aria-label="Main" className="order-3 flex w-full gap-1 md:order-none md:w-auto md:flex-1 lg:flex-none">
      {NAV.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            cn(
              'inline-flex h-10 items-center rounded-lg px-3.5 text-[15px] transition-colors',
              isActive ? 'bg-ink-850 font-semibold text-ice-100' : 'font-medium text-muted hover:text-ice-100',
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** "Search files and titles": submits to History with `?q=`. Collapses to an icon on phones. */
function TopSearch() {
  const navigate = useNavigate();
  const id = useId();
  const [text, setText] = useState('');
  const [expanded, setExpanded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    navigate(historySearchLink(text));
    setText('');
    setExpanded(false);
  };

  return (
    <>
      <IconButton
        label={expanded ? 'Close search' : 'Search files and titles'}
        icon={expanded ? <X className="h-5 w-5" /> : <Search className="h-5 w-5" />}
        size="lg"
        tooltip={false}
        aria-expanded={expanded}
        aria-controls={id}
        className="order-2 ml-auto md:hidden"
        onClick={() => setExpanded((v) => !v)}
      />
      <form
        role="search"
        onSubmit={submit}
        className={cn(
          'relative order-4 w-full md:order-none md:block md:w-auto md:flex-[0_1_260px]',
          expanded ? 'block' : 'hidden',
        )}
      >
        <label htmlFor={id} className="sr-only">
          Search files
        </label>
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
        <input
          ref={inputRef}
          id={id}
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setExpanded(false);
          }}
          placeholder="Search files and titles"
          className={cn(controlBase, 'h-10 pl-9 pr-3 text-sm')}
        />
      </form>
    </>
  );
}

function TopBar({ onImport }: { onImport: () => void }) {
  const { pathname } = useLocation();
  const onSettings = pathname === '/settings' || pathname.startsWith('/settings/');
  // History has its own search box; Settings shows the auto-save note instead of actions.
  const showSearch = !onSettings && pathname !== '/history';

  return (
    <header className="shrink-0 border-b border-ink-800 bg-ink-900">
      {/* Phones: brand + actions, then the nav, then search (one wrapping row). From lg: brand left,
          tabs centered, actions right (three columns). */}
      <div className="mx-auto flex max-w-[1360px] flex-wrap items-center gap-x-2 gap-y-2 px-4 py-2.5 md:gap-x-6 md:px-6 md:py-3.5 lg:grid lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <Brand />
        <MainNav />
        {/* `contents` below lg so each action keeps its own place in the wrapping row. */}
        <div className="contents lg:flex lg:min-w-0 lg:items-center lg:justify-end lg:gap-6">
          {onSettings ? (
            <span className="order-2 ml-auto flex items-center gap-2 text-[13px] text-muted md:order-none">
              <Check className="h-3.5 w-3.5 shrink-0 text-success" strokeWidth={2.5} aria-hidden />
              <span className="hidden sm:inline">Changes save automatically</span>
              <span className="sm:hidden">Saves automatically</span>
            </span>
          ) : (
            <>
              {showSearch && <TopSearch />}
              <Button
                variant="primary"
                size="lg"
                onClick={onImport}
                className={cn('order-2 px-4 md:order-none md:px-[18px]', !showSearch && 'ml-auto')}
              >
                Import files
              </Button>
            </>
          )}
        </div>
      </div>
    </header>
  );
}

function Banners() {
  const { data } = useAuthStatus();
  if (!data) return null;
  return (
    <>
      {!data.auth_enabled && (
        <div className="flex items-center gap-2 border-b border-warning/30 bg-warning/10 px-4 py-1.5 text-xs text-warning" role="note">
          <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
          <span>Authentication is disabled (SCHEMA_AUTH=disabled). Anyone on this network can rename and move files.</span>
        </div>
      )}
      {data.demo && (
        <div className="flex items-center gap-2 border-b border-brand-sky/20 bg-brand-sky/[0.07] px-4 py-1.5 text-xs text-accent" role="note">
          <FlaskConical className="h-3.5 w-3.5 shrink-0" />
          <span>Demo mode: a fictional library matched against an offline catalogue. Nothing outside the demo folders is touched.</span>
        </div>
      )}
    </>
  );
}

/** Top bar (Home · History · Settings, search, Import files) over a scrolling page. No sidebar. */
export function AppShell() {
  const { data } = useAuthStatus();
  const [params, setParams] = useSearchParams();
  useLiveUpdates(!!data && (!data.auth_enabled || data.authenticated));

  // Warm the lazily loaded pages once the shell is idle, so route changes render at once.
  useEffect(() => preloadRoutes(), []);

  // The import drawer lives in the URL (`?import=1`), so `/import` and links can open it.
  const importOpen = params.get('import') === '1';
  const openImport = () => {
    const next = new URLSearchParams(params);
    next.set('import', '1');
    setParams(next);
  };
  const closeImport = () => {
    const next = new URLSearchParams(params);
    next.delete('import');
    setParams(next, { replace: true });
  };

  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden bg-ink-950">
      <a
        href="#main"
        className="sr-only z-[300] rounded-md bg-ink-850 px-3 py-2 focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        Skip to content
      </a>
      <TopBar onImport={openImport} />
      <Banners />
      <main id="main" className="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
        <Outlet />
      </main>
      <ImportDrawer open={importOpen} onClose={closeImport} />
    </div>
  );
}
