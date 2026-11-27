import { useEffect, useState, type FormEvent, type ReactNode, type Ref } from 'react';
import { AlertTriangle, Check, CircleSlash, FileText, Paperclip, RotateCcw, Search, Undo2, Wand2 } from 'lucide-react';
import { useProviders, useRematchItem, useSearchItem, useUpdateItem } from '@/api/hooks';
import type { Candidate, EpisodeOrder, JobItemOut, MatchHint, MediaKind } from '@/api/types';
import { EPISODE_ORDERS } from '@/api/types';
import { cn } from '@/lib/cn';
import { parseIntList } from '@/lib/format';
import { KIND_LABEL, providerLabel, warningText, warningTone } from '@/lib/labels';
import { basename, splitPath } from '@/lib/pathDiff';
import {
  Button,
  ConfidenceMeter,
  EmptyState,
  Field,
  FileName,
  Input,
  ItemStatusPill,
  PathText,
  Select,
} from '@/components/ui';
import { CandidateList } from './CandidateList';
import { ParsedChips } from './ItemRow';
import { candidateRef, selectedCandidate } from './model';

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="border-b border-ink-800 px-4 py-3.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-2xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

const KIND_OPTIONS: { value: MediaKind; label: string }[] = [
  { value: 'movie', label: 'Movie' },
  { value: 'series', label: 'Series' },
  { value: 'anime', label: 'Anime' },
];

export interface InspectorProps {
  item: JobItemOut;
  searchRef?: Ref<HTMLInputElement>;
}

/** Right-hand inspector for one item (SPEC §9.3). */
export function Inspector({ item, searchRef }: InspectorProps) {
  const update = useUpdateItem();
  const rematch = useRematchItem();
  const search = useSearchItem();
  const providers = useProviders();
  const p = item.parsed;
  const current = selectedCandidate(item);

  /* ---------------- destination */
  const [dest, setDest] = useState(item.proposed_path ?? '');
  useEffect(() => setDest(item.proposed_path ?? ''), [item.id, item.proposed_path]);
  const destDirty = dest.trim() !== (item.proposed_path ?? '') && dest.trim().length > 0;

  /* ---------------- search */
  const initialKind: MediaKind = p.kind === 'unknown' ? (current?.kind ?? 'series') : p.kind;
  const [query, setQuery] = useState(p.title ?? '');
  const [year, setYear] = useState(p.year ? String(p.year) : '');
  const [searchKind, setSearchKind] = useState<MediaKind>(initialKind);
  const [provider, setProvider] = useState('any');
  const [results, setResults] = useState<Candidate[] | null>(null);

  /* ---------------- overrides */
  const [ovKind, setOvKind] = useState<MediaKind>(initialKind);
  const [season, setSeason] = useState(p.season !== null ? String(p.season) : '');
  const [episodes, setEpisodes] = useState(p.episodes.join(', '));
  const [absolute, setAbsolute] = useState(p.absolute.join(', '));
  const [order, setOrder] = useState<EpisodeOrder>(current?.episode_order ?? (p.absolute.length && !p.episodes.length ? 'absolute' : 'aired'));

  useEffect(() => {
    // Reset per-item form state when the inspected item changes.
    setQuery(item.parsed.title ?? '');
    setYear(item.parsed.year ? String(item.parsed.year) : '');
    const k: MediaKind = item.parsed.kind === 'unknown' ? 'series' : item.parsed.kind;
    setSearchKind(k);
    setOvKind(k);
    setSeason(item.parsed.season !== null ? String(item.parsed.season) : '');
    setEpisodes(item.parsed.episodes.join(', '));
    setAbsolute(item.parsed.absolute.join(', '));
    setResults(null);
  }, [item.id, item.parsed]);

  const providerOptions = [
    { value: 'any', label: 'Any provider' },
    ...(providers.data?.statuses ?? [])
      .filter((s) => s.enabled && s.kinds.includes(searchKind))
      .map((s) => ({ value: s.name, label: s.label || providerLabel(s.name) })),
  ];

  const runSearch = (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    search.mutate(
      {
        id: item.id,
        body: {
          query: query.trim(),
          kind: searchKind,
          year: year ? Number(year) : null,
          provider: provider === 'any' ? null : provider,
        },
      },
      { onSuccess: setResults },
    );
  };

  const pickSearchResult = (c: Candidate) => {
    // Search results aren't stored on the item, so force them through rematch with hint.ref.
    const hint: MatchHint = { kind: c.kind, provider: c.provider, ref: candidateRef(c) };
    if (c.kind !== 'movie') {
      if (season) hint.season = Number(season);
      if (episodes) hint.episodes = parseIntList(episodes);
      if (absolute) hint.absolute = parseIntList(absolute);
      hint.episode_order = order;
    }
    rematch.mutate({ id: item.id, body: { hint } }, { onSuccess: () => setResults(null) });
  };

  const applyOverrides = (e: FormEvent) => {
    e.preventDefault();
    const hint: MatchHint = { kind: ovKind };
    if (ovKind !== 'movie') {
      if (season.trim()) hint.season = Number(season);
      const eps = parseIntList(episodes);
      const abs = parseIntList(absolute);
      if (eps.length) hint.episodes = eps;
      if (abs.length) hint.absolute = abs;
      hint.episode_order = order;
    }
    if (current) {
      hint.ref = candidateRef(current);
      hint.provider = current.provider;
    }
    rematch.mutate({ id: item.id, body: { hint } });
  };

  const setStatus = (status: 'approved' | 'skipped' | 'review') => update.mutate({ item, patch: { status } });

  const canApprove = ['review', 'matched', 'skipped'].includes(item.status) && !!item.proposed_path;
  const canSkip = ['review', 'matched', 'approved', 'pending'].includes(item.status);
  const canReview = ['approved', 'skipped', 'matched'].includes(item.status);
  const locked = item.status === 'executing' || item.status === 'done';
  const folder = splitPath(item.source_path);

  return (
    <div className="flex min-h-full flex-col">
      {/* header */}
      <div className="border-b border-ink-800 px-4 py-3.5">
        <div className="mb-1.5 flex items-center gap-2">
          <ItemStatusPill status={item.status} />
          <ConfidenceMeter value={item.confidence} />
          {item.path_overridden && <span className="text-2xs text-muted">· path edited</span>}
        </div>
        <FileName name={item.filename} className="whitespace-normal break-all text-[13px] leading-5" />
        <p className="mt-0.5 truncate font-mono text-2xs text-muted" title={item.source_path}>
          {folder.root}
          {folder.folders.join(folder.sep)}
        </p>
        {!locked && (
          <div className="mt-3 flex flex-wrap gap-2">
            {canApprove && item.status !== 'approved' && (
              <Button size="sm" variant="primary" leftIcon={<Check className="h-3.5 w-3.5" />} onClick={() => setStatus('approved')}>
                Approve
              </Button>
            )}
            {canSkip && item.status !== 'skipped' && (
              <Button size="sm" leftIcon={<CircleSlash className="h-3.5 w-3.5" />} onClick={() => setStatus('skipped')}>
                Skip
              </Button>
            )}
            {canReview && (
              <Button size="sm" variant="ghost" leftIcon={<Undo2 className="h-3.5 w-3.5" />} onClick={() => setStatus('review')}>
                Back to review
              </Button>
            )}
          </div>
        )}
      </div>

      {item.error && (
        <div className="border-b border-danger/30 bg-danger/10 px-4 py-2.5 text-xs text-danger" role="alert">
          {item.error}
        </div>
      )}

      {/* destination */}
      <Section
        title={item.final_path ? 'Final location' : 'Destination'}
        aside={
          item.path_overridden &&
          !locked && (
            <Button
              size="xs"
              variant="subtle"
              leftIcon={<RotateCcw className="h-3 w-3" />}
              onClick={() => update.mutate({ item, patch: { reset_path: true } })}
            >
              Reset to format
            </Button>
          )
        }
      >
        <div className="mb-2 rounded-md bg-ink-950 px-2 py-1.5">
          <PathText path={item.final_path ?? item.proposed_path} compareTo={item.filename} wrap placeholder="No destination yet" />
        </div>
        {item.folder_rename && (
          <p className="mb-2 text-2xs text-muted">
            {item.status === 'done' ? 'Renamed' : 'Renames'} the folder{' '}
            <span className="font-mono text-ice-100/90">{basename(item.folder_rename.source)}</span> to{' '}
            <span className="font-mono text-ice-100/90">{basename(item.folder_rename.destination)}</span> in place;
            anything else in it stays with it.
          </p>
        )}
        {!locked && (
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (destDirty) update.mutate({ item, patch: { proposed_path: dest.trim() } });
            }}
          >
            <Input
              mono
              size="sm"
              className="flex-1"
              value={dest}
              onChange={(e) => setDest(e.target.value)}
              aria-label="Edit destination path"
              placeholder="Absolute, or relative to the library root"
            />
            <Button size="sm" type="submit" disabled={!destDirty}>
              Save
            </Button>
          </form>
        )}
      </Section>

      {/* warnings */}
      {item.warnings.length > 0 && (
        <Section title={`Warnings (${item.warnings.length})`}>
          <ul className="flex flex-col gap-1.5">
            {item.warnings.map((w) => (
              <li
                key={w}
                className={cn(
                  'flex items-start gap-2 rounded-md px-2 py-1.5 text-xs',
                  warningTone(w) === 'danger' ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning',
                )}
              >
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                <span className="break-words">{warningText(w)}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* parsed */}
      <Section title="Parsed from the filename">
        <ParsedChips item={item} wrap />
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          <dt className="text-muted">Title</dt>
          <dd className="truncate">{p.title ?? '—'}</dd>
          {p.alt_titles.length > 0 && (
            <>
              <dt className="text-muted">Also</dt>
              <dd className="truncate">{p.alt_titles.join(', ')}</dd>
            </>
          )}
          {p.special_title && (
            <>
              <dt className="text-muted">Special</dt>
              <dd className="truncate">{p.special_title}</dd>
            </>
          )}
          {(p.quality.video_codec || p.quality.audio_codec) && (
            <>
              <dt className="text-muted">Codecs</dt>
              <dd className="truncate font-mono text-2xs leading-5">
                {[p.quality.video_codec, p.quality.bit_depth ? `${p.quality.bit_depth}bit` : null, ...p.quality.hdr, p.quality.audio_codec, p.quality.audio_channels, p.quality.atmos ? 'Atmos' : null]
                  .filter(Boolean)
                  .join(' · ')}
              </dd>
            </>
          )}
          {p.crc32 && (
            <>
              <dt className="text-muted">CRC32</dt>
              <dd className="font-mono text-2xs leading-5">{p.crc32}</dd>
            </>
          )}
        </dl>
      </Section>

      {/* candidates */}
      <Section
        title={`Candidates (${item.candidate_count ?? item.candidates.length})`}
        aside={item.slim && <span className="text-2xs text-muted">Loading details…</span>}
      >
        {item.candidates.length === 0 ? (
          <p className="text-xs text-muted">No provider returned a match. Search below or set overrides.</p>
        ) : (
          <CandidateList
            candidates={item.candidates}
            selectedId={current?.id ?? null}
            busyId={update.isPending ? (update.variables?.patch.selected_candidate_id ?? null) : null}
            onPick={(c) => !locked && c.id !== current?.id && update.mutate({ item, patch: { selected_candidate_id: c.id } })}
          />
        )}
      </Section>

      {/* search instead */}
      {!locked && (
        <Section title="Search instead">
          <form onSubmit={runSearch} className="flex flex-col gap-2">
            <Input
              ref={searchRef}
              size="sm"
              leftIcon={<Search />}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                // Hand the keyboard back to the file list.
                if (e.key === 'Escape') e.currentTarget.blur();
              }}
              placeholder="Title"
              aria-label="Search title"
            />
            <div className="grid grid-cols-[72px_1fr_1fr] gap-2">
              <Input
                size="sm"
                inputMode="numeric"
                value={year}
                onChange={(e) => setYear(e.target.value.replace(/\D/g, '').slice(0, 4))}
                placeholder="Year"
                aria-label="Year"
              />
              <Select size="sm" value={searchKind} onChange={setSearchKind} options={KIND_OPTIONS} aria-label="Kind" />
              <Select size="sm" value={provider} onChange={setProvider} options={providerOptions} aria-label="Provider" />
            </div>
            <Button size="sm" type="submit" loading={search.isPending} disabled={!query.trim()}>
              Search
            </Button>
          </form>
          {results && (
            <div className="mt-3">
              {results.length === 0 ? (
                <EmptyState compact title="No results" description="Try another spelling, drop the year, or switch provider." />
              ) : (
                <CandidateList
                  candidates={results}
                  selectedId={null}
                  pickLabel="Match to"
                  busyId={rematch.isPending ? 'pending' : null}
                  onPick={pickSearchResult}
                />
              )}
            </div>
          )}
        </Section>
      )}

      {/* overrides */}
      {!locked && (
        <Section title="Overrides">
          <form onSubmit={applyOverrides} className="flex flex-col gap-2.5">
            <div className="grid grid-cols-2 gap-2">
              <Field label="Kind">
                <Select size="sm" value={ovKind} onChange={setOvKind} options={KIND_OPTIONS} />
              </Field>
              {ovKind !== 'movie' && (
                <Field label="Episode order">
                  <Select<EpisodeOrder>
                    size="sm"
                    value={order}
                    onChange={setOrder}
                    options={EPISODE_ORDERS.map((o) => ({ value: o, label: o[0].toUpperCase() + o.slice(1) }))}
                  />
                </Field>
              )}
            </div>
            {ovKind !== 'movie' && (
              <div className="grid grid-cols-[64px_1fr_1fr] gap-2">
                <Field label="Season">
                  <Input size="sm" inputMode="numeric" value={season} onChange={(e) => setSeason(e.target.value.replace(/\D/g, ''))} />
                </Field>
                <Field label="Episode(s)">
                  <Input size="sm" value={episodes} onChange={(e) => setEpisodes(e.target.value)} placeholder="2, 3" />
                </Field>
                <Field label="Absolute">
                  <Input size="sm" value={absolute} onChange={(e) => setAbsolute(e.target.value)} placeholder="15" />
                </Field>
              </div>
            )}
            <Button size="sm" type="submit" leftIcon={<Wand2 className="h-3.5 w-3.5" />} loading={rematch.isPending}>
              Re-match with overrides
            </Button>
            {current && (
              <p className="text-2xs text-muted">
                Keeps {current.title} ({providerLabel(current.provider)}) and re-resolves the episode with these numbers.
              </p>
            )}
          </form>
        </Section>
      )}

      {/* sidecars */}
      <Section title={`Sidecars (${item.sidecars.length})`}>
        {item.sidecars.length === 0 ? (
          <p className="text-xs text-muted">No subtitles or companion files found.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {item.sidecars.map((s) => (
              <li key={s} className="flex items-center gap-2 font-mono text-xs" title={s}>
                {/\.(srt|ass|ssa|sub|idx|sup|vtt)$/i.test(s) ? (
                  <Paperclip className="h-3 w-3 shrink-0 text-muted" />
                ) : (
                  <FileText className="h-3 w-3 shrink-0 text-muted" />
                )}
                <span className="truncate">{basename(s)}</span>
              </li>
            ))}
          </ul>
        )}
        {item.sidecars.length > 0 && <p className="mt-1.5 text-2xs text-muted">These travel with the video and keep their suffixes.</p>}
      </Section>
      <div className="px-4 py-3 text-2xs text-muted">
        Kind guessed as {KIND_LABEL[p.kind]} with {Math.round(p.confidence * 100)}% parser confidence.
      </div>
    </div>
  );
}
