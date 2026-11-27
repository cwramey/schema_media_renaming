import { useEffect, useRef, type MouseEvent } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useFormatCatalog, useFormats, useProviders, useSettings, useWatchFolders } from '@/api/hooks';
import { useDocumentTitle, usePrefersReducedMotion } from '@/lib/hooks';
import { SaveIndicator } from './controls';
import { SECTION_IDS, TAB_TO_SECTION, sectionFromHash, type SectionId } from './mapping';
import { FoldersSection } from './sections/FoldersSection';
import { NamingSection } from './sections/NamingSection';
import { MatchingSection } from './sections/MatchingSection';
import { MovingSection } from './sections/MovingSection';
import { ConnectionsSection } from './sections/ConnectionsSection';
import { DownloadsSection } from './sections/DownloadsSection';
import { FileTypesSection } from './sections/FileTypesSection';
import { AccountSection } from './sections/AccountSection';

const SECTION_TITLES: Record<SectionId, string> = {
  folders: 'Folders & watching',
  naming: 'Naming',
  matching: 'Matching',
  moving: 'Moving files',
  connections: 'Connections',
  downloads: 'Download clients',
  files: 'File types',
  account: 'Account & app',
};

function scrollToSection(id: string, smooth: boolean) {
  const el = document.getElementById(id);
  if (!el) return false;
  el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
  document.getElementById(`${id}-h`)?.focus({ preventScroll: true });
  return true;
}

/** Settings: one long page, eight sections in the order a file travels. Everything autosaves. */
export default function SettingsPage() {
  useDocumentTitle('Settings');
  const location = useLocation();
  const navigate = useNavigate();
  const { tab } = useParams();
  const reduced = usePrefersReducedMotion();

  // Data the tall sections need before their final height is known.
  const settings = useSettings();
  const watch = useWatchFolders();
  const providers = useProviders();
  const formats = useFormats();
  const catalog = useFormatCatalog();
  const ready = ![settings, watch, providers, formats, catalog].some((q) => q.isPending);

  // Old /settings/:tab links land on the section that replaced the tab.
  const target = sectionFromHash(location.hash) ?? (tab ? (TAB_TO_SECTION[tab] ?? null) : null);
  const scrolledFor = useRef<string | null>(null);
  const jumped = useRef(false);
  useEffect(() => {
    if (!ready || !target || scrolledFor.current === location.key) return;
    if (jumped.current) {
      // The chip nav already scrolled (smoothly); this location change is just its URL update.
      jumped.current = false;
      scrolledFor.current = location.key;
      return;
    }
    const key = location.key;
    // Let the freshly rendered rows lay out first, then once more after late content (previews,
    // connection checks) has settled, unless the reader has started scrolling by then.
    let userMoved = false;
    const stop = () => {
      userMoved = true;
    };
    const events = ['wheel', 'touchmove', 'keydown', 'pointerdown'] as const;
    events.forEach((e) => window.addEventListener(e, stop, { passive: true, once: true }));
    const raf = requestAnimationFrame(() => {
      scrolledFor.current = key;
      scrollToSection(target, false);
    });
    const timer = setTimeout(() => {
      if (!userMoved) scrollToSection(target, false);
    }, 600);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(timer);
      events.forEach((e) => window.removeEventListener(e, stop));
    };
  }, [ready, target, location.key]);

  const jump = (e: MouseEvent<HTMLAnchorElement>, id: SectionId) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    jumped.current = true;
    navigate({ pathname: '/settings', hash: `#${id}` }, { replace: true });
    scrollToSection(id, !reduced);
  };

  return (
    <div className="mx-auto w-full max-w-[960px] px-4 pb-24 pt-6 md:px-6 md:pt-9">
      <header className="mb-7 flex flex-col gap-3.5">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
          <div className="min-w-0 flex-[1_1_320px]">
            <h1 className="text-2xl font-bold tracking-tight text-ice-100 md:text-[30px] md:leading-tight">Settings</h1>
            <p className="mt-1 text-[13px] text-muted md:text-sm">
              Every option in one place, top to bottom in the order a file travels. Each one says what it does in plain words.
            </p>
          </div>
          <SaveIndicator />
        </div>
        <nav aria-label="Jump to section" className="flex flex-wrap gap-2">
          {SECTION_IDS.map((id, i) => (
            <a
              key={id}
              href={`#${id}`}
              onClick={(e) => jump(e, id)}
              className="inline-flex min-h-9 items-center rounded-full border border-ink-800 bg-ink-900 px-3 text-[13px] font-medium text-ice-100/80 transition-colors hover:border-ink-700 hover:text-ice-100"
            >
              {i + 1} · {SECTION_TITLES[id]}
            </a>
          ))}
        </nav>
      </header>
      <div className="flex flex-col gap-6">
        <FoldersSection />
        <NamingSection />
        <MatchingSection />
        <MovingSection />
        <ConnectionsSection />
        <DownloadsSection />
        <FileTypesSection />
        <AccountSection />
      </div>
    </div>
  );
}
