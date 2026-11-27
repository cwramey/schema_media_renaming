import { useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, ArrowRight, Check, ChevronDown, ChevronRight, File, Film, FolderOpen, RotateCw, Tv } from 'lucide-react';
import { useDismissAllHome, useHome, useHomeAction, useRefreshHome, useStartNow } from '@/api/hooks';
import type { HomeOut, IncomingFile, LibraryCard, MediaKind, NeedsYouCard, RenamingCard } from '@/api/types';
import { cn } from '@/lib/cn';
import { formatBytes, formatNumber } from '@/lib/format';
import type { Tone } from '@/lib/labels';
import { useDocumentTitle, useNow } from '@/lib/hooks';
import { itemLink } from '@/lib/routes';
import { toast } from '@/lib/toast';
import { Button, ButtonLink, ConfirmDialog, ErrorState, ProgressBar, Skeleton } from '@/components/ui';
import { relativeDay } from '@/features/history/days';
import { UndoOperationButton } from '@/features/history/UndoOperationButton';
import { clockSkew, formatCountdown, hasSettleWait, settleProgress } from './countdown';
import {
  ACTION_DONE,
  episodeCode,
  episodeRange,
  libraryCaption,
  libraryRows,
  needsYouHeading,
  parentFolder,
  primaryAction,
  statusChips,
  totalsLabel,
  type LibraryRow,
} from './status';

const SUBTITLE_CLEAR = 'Files move from Incoming to your library on their own. You only step in when something lands in Needs review.';
const SUBTITLE_NEEDS = 'Everything else moves on its own. Pick a match or skip each file in Needs review.';

/** Home: headline, service status, the pipeline (Incoming → Needs review → Renaming) and what was organized lately. */
export function HomePage() {
  const home = useHome();
  const data = home.data;
  useDocumentTitle(data && data.needs_you_count > 0 ? `${data.needs_you_count} need review` : 'Home');

  // Countdowns tick on the server's clock (corrected for skew at fetch time), each in its own
  // leaf component, so the page itself renders only when the data changes.
  const skew = data ? clockSkew(data.server_time, home.dataUpdatedAt) : 0;

  return (
    <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-7 px-4 pb-8 pt-6 md:px-6 md:pt-8">
      {!data ? (
        home.isError ? (
          <ErrorState error={home.error} onRetry={() => void home.refetch()} title="Could not load Home" />
        ) : (
          <HomeSkeleton />
        )
      ) : (
        <>
          <div className="flex flex-col gap-4">
            <Hero count={data.needs_you_count} />
            {home.isError && (
              <p role="status" className="-mt-1 text-[13px] text-warning">
                Can’t reach Schema right now. Showing the last update; trying again…
              </p>
            )}
            <StatusStrip data={data} />
          </div>
          <Pipeline data={data} skew={skew} />
          <Library data={data} />
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ headline + status */

function Hero({ count }: { count: number }) {
  // Only a click shows the spinner; background polls stay quiet.
  const refresh = useRefreshHome();
  const needs = count > 0;
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <h1 className="flex items-center gap-2.5 font-display text-[26px] font-bold leading-8 tracking-[-0.02em] [text-wrap:balance] md:text-[32px] md:leading-10">
          <span
            className={cn('grid h-7 w-7 shrink-0 place-items-center rounded-full text-ink-950', needs ? 'bg-warning' : 'bg-success')}
            aria-hidden
          >
            {needs ? <AlertTriangle className="h-4 w-4" strokeWidth={2.5} /> : <Check className="h-4 w-4" strokeWidth={3} />}
          </span>
          {needsYouHeading(count)}
        </h1>
        <p className="mt-1.5 max-w-[62ch] text-sm text-muted">{needs ? SUBTITLE_NEEDS : SUBTITLE_CLEAR}</p>
      </div>
      <Button
        variant="secondary"
        size="md"
        loading={refresh.isPending}
        leftIcon={<RotateCw className="h-3.5 w-3.5" aria-hidden />}
        onClick={() =>
          refresh.mutate(undefined, {
            onSuccess: ({ cleared, started }) => {
              if (started > 0) {
                toast.success(`Started ${started} file${started === 1 ? '' : 's'}`, {
                  description: 'Found in the downloads folder.',
                });
              }
              if (cleared > 0) {
                toast.success(`Cleared ${cleared} file${cleared === 1 ? '' : 's'}`, {
                  description: 'They are no longer in the downloads folder.',
                });
              }
            },
          })
        }
      >
        Refresh
      </Button>
    </div>
  );
}

const CHIP_TONE: Record<Tone, string> = {
  success: 'bg-success/10 text-success',
  warning: 'bg-warning/10 text-warning',
  danger: 'bg-danger/10 text-danger',
  info: 'bg-info/10 text-info',
  brand: 'bg-accent/10 text-accent',
  neutral: 'bg-ink-850 text-muted',
};

function StatusStrip({ data }: { data: HomeOut }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-[10px] border border-ink-800 bg-ink-900 px-2.5 py-2">
      {statusChips(data.status).map((c) => (
        <span
          key={c.key}
          className={cn(
            'inline-flex min-w-0 items-center gap-1.5 rounded-full py-1 pl-2 pr-2.5 text-xs font-medium [overflow-wrap:anywhere]',
            CHIP_TONE[c.tone],
          )}
        >
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current" aria-hidden />
          {c.label}
        </span>
      ))}
      <span className="tabular flex w-full items-center gap-1.5 px-1 font-mono text-xs text-muted sm:ml-auto sm:w-auto">
        <FolderOpen className="h-4 w-4 shrink-0" aria-hidden />
        {totalsLabel(data.library.total_files, data.library.total_bytes)}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ pipeline */

function SectionHead({ id, title, children }: { id: string; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
      <h2 id={id} className="flex items-center gap-2.5 font-display text-xl font-semibold tracking-[-0.01em]">
        {title}
      </h2>
      {children}
    </div>
  );
}

function Pipeline({ data, skew }: { data: HomeOut; skew: number }) {
  const id = useId();
  const idle = data.incoming.length === 0 && data.needs_you_count === 0 && data.renaming.length === 0;
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <SectionHead id={id} title="Pipeline">
        <span className="text-[13px] text-muted">{idle ? 'Nothing in progress' : 'Left to right, on its own'}</span>
      </SectionHead>
      {idle ? <QuietRail /> : <Board data={data} skew={skew} />}
    </section>
  );
}

/** Nothing in flight: one quiet row instead of three empty columns. */
function QuietRail() {
  const stages = [
    ['Incoming', 'No downloads waiting'],
    ['Needs review', 'Nothing to check'],
    ['Renaming', 'Nothing moving'],
  ];
  return (
    <ol className="grid rounded-[10px] border border-ink-800 bg-ink-900 md:grid-cols-[1fr_auto_1fr_auto_1fr] md:items-center">
      {stages.map(([name, empty], i) => (
        <li key={name} className="contents">
          {i > 0 && <ChevronRight className="hidden h-4 w-4 text-ink-700 md:block" aria-hidden />}
          <div className={cn('flex min-w-0 items-center gap-2.5 px-4 py-3', i > 0 && 'border-t border-ink-800 md:border-t-0')}>
            <span className="font-semibold">{name}</span>
            <span className="truncate text-[13px] text-muted">{empty}</span>
            <span className="tabular ml-auto rounded-full border border-ink-800 bg-ink-850 px-2 py-0.5 font-mono text-xs text-muted">0</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

/** Review cards shown before "Show more", so a long list doesn't push the library off the page. */
const NEEDS_PREVIEW = 5;

function Board({ data, skew }: { data: HomeOut; skew: number }) {
  const [allNeeds, setAllNeeds] = useState(false);
  const needs = allNeeds ? data.needs_you : data.needs_you.slice(0, NEEDS_PREVIEW);
  const hidden = data.needs_you.length - needs.length;
  const shownNeeds = needs.length;
  return (
    <div className="grid items-start gap-3.5 md:grid-cols-3">
      <Column title="Incoming" count={data.incoming.length} caption="Waiting for downloads to finish">
        {data.incoming.length === 0 ? (
          <EmptyColumn>No downloads waiting</EmptyColumn>
        ) : (
          data.incoming.map((f) => <IncomingItem key={`${f.folder_id}:${f.path}`} file={f} skew={skew} />)
        )}
      </Column>

      <Column
        title="Needs review"
        count={data.needs_you_count}
        caption="Unsure matches and conflicts"
        attention={data.needs_you_count > 0}
      >
        {shownNeeds === 0 ? (
          <EmptyColumn>Nothing needs review</EmptyColumn>
        ) : (
          <>
            {needs.map((c) => (
              <NeedsYouItem key={c.key} card={c} />
            ))}
            {hidden > 0 && (
              <Button variant="outline" size="md" block onClick={() => setAllNeeds(true)}>
                Show {hidden} more
              </Button>
            )}
            <div className="flex items-center justify-between gap-2 px-0.5 text-xs text-muted">
              <span>
                {data.needs_you_count > shownNeeds
                  ? `Showing the newest ${shownNeeds} of ${formatNumber(data.needs_you_count)}`
                  : 'Newest first'}
              </span>
              <DismissAllButton count={data.needs_you_count} />
            </div>
          </>
        )}
      </Column>

      <Column title="Renaming" count={data.renaming.length} caption="Matched and being moved now">
        {data.renaming.length === 0 ? (
          <EmptyColumn>Nothing moving right now</EmptyColumn>
        ) : (
          data.renaming.map((r) => <RenamingItem key={r.item_id} card={r} />)
        )}
      </Column>
    </div>
  );
}

function Column({
  title,
  count,
  caption,
  attention,
  children,
}: {
  title: string;
  count: number;
  caption: string;
  /** Amber only while something waits on you, so amber always means "act now". */
  attention?: boolean;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section
      aria-labelledby={id}
      className={cn(
        'flex min-w-0 flex-col gap-2.5 rounded-xl border border-ink-800 bg-ink-900 p-3.5',
        // Phones: the one column you act on comes first.
        attention && 'max-md:order-first border-warning/45 bg-warning/[0.06]',
      )}
    >
      <h3 id={id} className={cn('flex justify-between gap-2 px-0.5 text-sm font-semibold', attention && 'text-warning')}>
        {title}
        <span className={cn('tabular font-mono text-xs', !attention && 'text-muted')}>{formatNumber(count)}</span>
      </h3>
      <p className="-mt-1.5 mb-0.5 px-0.5 text-xs text-muted">{caption}</p>
      {children}
    </section>
  );
}

function EmptyColumn({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[10px] border border-dashed border-ink-700 px-3.5 py-6 text-center text-[13px] text-muted">
      {children}
    </div>
  );
}

const cardClass = 'flex min-w-0 flex-col gap-2 rounded-[10px] border border-ink-800 bg-ink-850 p-3';
const wrapAnywhere = '[overflow-wrap:anywhere]';

function IncomingItem({ file, skew }: { file: IncomingFile; skew: number }) {
  const title = file.title_guess ?? file.name;
  return (
    <article className={cardClass}>
      <div className={cn('font-semibold', wrapAnywhere)} title={file.name}>
        {title}
      </div>
      {file.state === 'partial' || !hasSettleWait(file) ? (
        <>
          <div className="text-[13px] text-muted">
            Still downloading{file.size > 0 ? ` · ${formatBytes(file.size)} so far` : ''}
          </div>
          <ProgressBar label={`${title}: still downloading`} className="h-[5px]" />
        </>
      ) : (
        <SettleCountdown file={file} title={title} skew={skew} />
      )}
    </article>
  );
}

/** The ticking part of a settling download: countdown, progress and "Start now" (re-renders every second). */
function SettleCountdown({ file, title, skew }: { file: IncomingFile; title: string; skew: number }) {
  const start = useStartNow();
  const progress = settleProgress(file, useNow(1000) + skew);
  if (!progress) return null;
  const starting = start.isPending || start.isSuccess || progress.due;
  return (
    <>
      <div className="text-[13px] text-muted">
        Download finished · {starting ? 'starting now' : `starts in ${formatCountdown(progress.remainingMs)}`}
      </div>
      <ProgressBar value={starting ? 1 : progress.fraction} label={`${title}: wait before processing`} className="h-[5px]" />
      <Button
        variant="outline"
        size="md"
        className="self-start"
        loading={start.isPending}
        disabled={starting}
        aria-label={`Start ${title} now`}
        onClick={() => start.mutate({ folderId: file.folder_id, path: file.path })}
      >
        Start now
      </Button>
    </>
  );
}

function NeedsYouItem({ card }: { card: NeedsYouCard }) {
  const act = useHomeAction();
  const primary = primaryAction(card);
  const matchLink = card.kind === 'item' && card.job_id !== null ? itemLink(card.job_id, card.item_id) : null;

  return (
    <article className={cardClass}>
      <div className={cn('font-mono text-[13px] font-medium', wrapAnywhere)}>{card.title}</div>
      <div className="text-[13px] text-muted">{card.reason}</div>
      {(primary || matchLink) && (
        <div className="flex flex-wrap gap-1.5">
          {primary && (
            <Button
              variant="primary"
              size="md"
              // Labels like "Use Terminator: The Sarah Connor Chronicles (2008)" wrap inside the card.
              className={cn('!h-auto min-h-8 max-w-full !whitespace-normal py-1.5 text-left', wrapAnywhere)}
              loading={act.isPending}
              onClick={() =>
                act.mutate(
                  { key: card.key, action: primary.action },
                  { onSuccess: () => toast.success(ACTION_DONE[primary.action], { description: card.title }) },
                )
              }
            >
              {primary.label}
            </Button>
          )}
          {matchLink && (
            <ButtonLink to={matchLink} variant="outline" size="md">
              Choose match
            </ButtonLink>
          )}
        </div>
      )}
    </article>
  );
}

/** Clears the whole Needs review list (every card, not just the ones shown) after a confirmation. */
function DismissAllButton({ count }: { count: number }) {
  const dismissAll = useDismissAllHome();
  const [confirm, setConfirm] = useState(false);
  const files = count === 1 ? '1 file' : `${formatNumber(count)} files`;
  return (
    <>
      <Button variant="subtle" size="md" onClick={() => setConfirm(true)}>
        Clear list
      </Button>
      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          dismissAll.mutate(undefined, {
            onSuccess: () => toast.success('List cleared'),
            onSettled: () => setConfirm(false),
          })
        }
        loading={dismissAll.isPending}
        title={count > 0 ? `Clear all ${files} from the list?` : 'Clear the list?'}
        description="Every card leaves Needs review, including any not shown here. The files themselves are not moved or deleted."
        confirmLabel="Clear list"
      />
    </>
  );
}

const RENAMING_STATUS: Record<RenamingCard['status'], string> = {
  matching: 'Finding a match…',
  matched: 'Matched · moving soon',
  approved: 'Approved · moving soon',
  executing: 'Moving…',
};

function RenamingItem({ card }: { card: RenamingCard }) {
  const active = card.status === 'matching' || card.status === 'executing';
  return (
    <article className={cardClass}>
      <Link to={itemLink(card.job_id, card.item_id)} className={cn('font-semibold hover:underline', wrapAnywhere)}>
        {card.title}
      </Link>
      <div className="truncate font-mono text-xs text-muted" title={card.filename}>
        {card.filename}
      </div>
      <div className="text-[13px] text-muted">{RENAMING_STATUS[card.status]}</div>
      {active && (
        <ProgressBar
          value={card.job_progress !== null && card.job_progress > 0 ? card.job_progress : undefined}
          label={`${card.title}: ${RENAMING_STATUS[card.status]}`}
          className="h-[5px]"
        />
      )}
    </article>
  );
}

/* ------------------------------------------------------------------ library */

function Library({ data }: { data: HomeOut }) {
  const id = useId();
  const rows = libraryRows(data.library.recent);
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <SectionHead
        id={id}
        title={
          <>
            Recently organized
            <span className="tabular rounded-full border border-ink-800 bg-ink-850 px-2 py-0.5 font-mono text-xs font-medium text-muted">
              {formatNumber(data.library.total_files)}
            </span>
          </>
        }
      >
        <span className="mr-auto text-[13px] text-muted">{libraryCaption(data.status.media_servers)}</span>
        <Link
          to="/history"
          className="inline-flex min-h-8 items-center gap-1 text-[13px] font-medium text-accent hover:underline"
        >
          See all in History
          <ArrowRight className="h-4 w-4" aria-hidden />
        </Link>
      </SectionHead>
      {rows.length === 0 ? (
        <EmptyColumn>Nothing organized yet. Files show up here once they reach your library.</EmptyColumn>
      ) : (
        <ul className="divide-y divide-ink-800 overflow-hidden rounded-xl border border-ink-800 bg-ink-900">
          {rows.map((row) => (
            <LibraryListRow key={row.type === 'group' ? row.key : row.card.operation_id} row={row} />
          ))}
        </ul>
      )}
    </section>
  );
}

const KIND_ICON: Record<MediaKind, typeof Film> = { movie: Film, series: Tv, anime: Tv, unknown: File };

function KindIcon({ kind }: { kind: MediaKind }) {
  const Icon = KIND_ICON[kind];
  return (
    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-ink-800 bg-ink-850 text-accent" aria-hidden>
      <Icon className="h-4 w-4" />
    </span>
  );
}

const rowGrid = 'grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-3.5 py-3 sm:grid-cols-[auto_minmax(0,1fr)_auto_auto] sm:gap-4 sm:pl-[18px] sm:pr-4';

/** Time and size, right-aligned from sm; on phones the time joins the path line instead. */
function When({ ts, bytes }: { ts: string; bytes: number }) {
  return (
    <div className="hidden whitespace-nowrap text-right text-[13px] text-muted sm:block">
      {relativeDay(ts)}
      {bytes > 0 && <span className="tabular block text-xs">{formatBytes(bytes)}</span>}
    </div>
  );
}

function PathLine({ path, ts }: { path: string; ts: string }) {
  return (
    <div className="mt-0.5 truncate font-mono text-xs text-muted" title={path}>
      {path}
      <span className="sm:hidden"> · {relativeDay(ts)}</span>
    </div>
  );
}

function Undo({ card, label }: { card: LibraryCard; label: string }) {
  if (!card.can_undo) return <span aria-hidden />;
  return (
    <UndoOperationButton
      operationId={card.operation_id}
      title={label}
      destination={card.destination}
      variant="subtle"
      compact
      className="h-9 px-3 text-[13px] max-sm:w-9 max-sm:px-0"
    />
  );
}

function LibraryListRow({ row }: { row: LibraryRow }) {
  const [open, setOpen] = useState(false);
  const subId = useId();

  if (row.type === 'single') {
    const { card } = row;
    const ep = card.show && card.season !== null && card.season !== undefined && card.episode !== null && card.episode !== undefined ? episodeCode(card.season, card.episode) : null;
    return (
      <li className={cn(rowGrid, 'transition-colors hover:bg-ink-850/60')}>
        <KindIcon kind={card.kind} />
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 text-[15px] font-semibold">
            <span className={wrapAnywhere}>{ep ? card.show : card.title}</span>
            {ep && <span className="font-mono text-xs font-medium text-accent">{ep}</span>}
          </div>
          <PathLine path={card.destination} ts={card.ts} />
        </div>
        <When ts={card.ts} bytes={card.bytes} />
        <Undo card={card} label={card.title} />
      </li>
    );
  }

  const first = row.cards[0];
  const bytes = row.cards.reduce((n, c) => n + c.bytes, 0);
  return (
    <li>
      <div className={cn(rowGrid, 'transition-colors hover:bg-ink-850/60')}>
        <KindIcon kind={first.kind} />
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 text-[15px] font-semibold">
            <span className={wrapAnywhere}>{row.show}</span>
            <span className="font-mono text-xs font-medium text-accent">{episodeRange(row.season, row.cards)}</span>
            <button
              type="button"
              aria-expanded={open}
              aria-controls={subId}
              onClick={() => setOpen((v) => !v)}
              className="inline-flex items-center gap-1 self-center rounded-md text-[13px] font-medium text-muted hover:text-ice-100"
            >
              <ChevronDown className={cn('h-4 w-4 transition-transform', open && 'rotate-180')} aria-hidden />
              {row.cards.length} episodes
            </button>
          </div>
          <PathLine path={parentFolder(first.destination)} ts={first.ts} />
        </div>
        <When ts={first.ts} bytes={bytes} />
        {/* Undo is per episode, inside the group. */}
        <span aria-hidden />
      </div>
      <ul id={subId} hidden={!open} className="divide-y divide-ink-800 border-t border-ink-800 bg-ink-950/60">
        {row.cards.map((card) => (
          <li key={card.operation_id} className={rowGrid}>
            {/* Lines the episode up under the show title. */}
            <span className="w-9" aria-hidden />
            <div className="min-w-0">
              <div className="font-mono text-xs font-medium text-accent">
                {card.season !== null && card.season !== undefined && card.episode !== null && card.episode !== undefined ? episodeCode(card.season, card.episode) : card.title}
              </div>
              <PathLine path={card.destination} ts={card.ts} />
            </div>
            <When ts={card.ts} bytes={card.bytes} />
            <Undo card={card} label={card.title} />
          </li>
        ))}
      </ul>
    </li>
  );
}

/* ------------------------------------------------------------------ skeleton */

function HomeSkeleton() {
  return (
    <div className="flex flex-col gap-7" aria-busy="true" aria-label="Loading Home">
      <div className="flex flex-col gap-4">
        <Skeleton className="h-9 w-64" />
        <Skeleton className="h-4 w-96 max-w-full" />
        <Skeleton className="h-10 rounded-[10px]" />
      </div>
      <div className="flex flex-col gap-3">
        <Skeleton className="h-6 w-28" />
        <Skeleton className="h-12 rounded-[10px]" />
      </div>
      <div className="flex flex-col gap-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-72 rounded-xl" />
      </div>
    </div>
  );
}
