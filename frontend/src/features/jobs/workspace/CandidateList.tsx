import { useState } from 'react';
import { Check, Film, Tv } from 'lucide-react';
import type { Candidate } from '@/api/types';
import { cn } from '@/lib/cn';
import { pad2 } from '@/lib/format';
import { providerLabel } from '@/lib/labels';
import { Badge, ConfidenceMeter } from '@/components/ui';
import { posterOf } from './model';

/** Poster thumbnail size in CSS px (`w-9` x `h-[54px]`). */
const POSTER_W = 36;
const POSTER_H = 54;

function Poster({ c }: { c: Candidate }) {
  // Remember which URL failed, so a row reused for another candidate tries its poster again.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const url = posterOf(c);
  if (url && url !== failedUrl) {
    return (
      <img
        src={url}
        alt=""
        width={POSTER_W}
        height={POSTER_H}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(url)}
        className="h-[54px] w-9 shrink-0 rounded object-cover ring-1 ring-ink-800"
      />
    );
  }
  const Icon = c.kind === 'movie' ? Film : Tv;
  return (
    <span className="flex h-[54px] w-9 shrink-0 items-center justify-center rounded bg-gradient-to-br from-ink-800 to-ink-850 text-muted ring-1 ring-ink-800">
      <Icon className="h-4 w-4" aria-hidden />
    </span>
  );
}

function episodeLine(c: Candidate): string | null {
  if (c.episodes.length === 0) return null;
  return c.episodes
    .map((e) => {
      const num =
        e.season !== null && e.episode !== null
          ? `S${pad2(e.season)}E${pad2(e.episode)}`
          : e.absolute !== null
            ? `#${e.absolute}`
            : e.air_date ?? '';
      return e.title ? `${num} · ${e.title}` : num;
    })
    .join('  &  ');
}

export function CandidateList({
  candidates,
  selectedId,
  onPick,
  pickLabel = 'Use this match',
  busyId,
}: {
  candidates: Candidate[];
  selectedId: string | null;
  onPick: (c: Candidate) => void;
  pickLabel?: string;
  busyId?: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return (
    <ul className="flex flex-col gap-1.5" role="radiogroup" aria-label="Candidates">
      {candidates.map((c, i) => {
        const selected = c.id === selectedId;
        const eps = episodeLine(c);
        const series = c.series;
        return (
          <li key={c.id}>
            <div
              className={cn(
                'group relative flex gap-3 rounded-lg border p-2 transition-colors',
                selected ? 'border-brand-sky/60 bg-brand-sky/[0.07]' : 'border-ink-800 hover:border-ink-700 hover:bg-ink-850/60',
              )}
            >
              <Poster c={c} />
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  aria-label={`${pickLabel}: ${c.title}${c.year ? ` (${c.year})` : ''}`}
                  disabled={busyId === c.id}
                  onClick={() => onPick(c)}
                  className="absolute inset-0 rounded-lg focus-visible:outline-offset-[-2px]"
                />
                <div className="pointer-events-none flex items-start gap-2">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-medium text-ice-100">
                      {i === 0 && !selected && <span className="mr-1 text-2xs font-semibold text-muted">#1</span>}
                      {c.title}
                      {c.year && <span className="ml-1 font-normal text-muted">({c.year})</span>}
                    </span>
                    <span className="mt-0.5 flex flex-wrap items-center gap-1">
                      <Badge tone="info">{providerLabel(c.provider)}</Badge>
                      {series?.network && <Badge>{series.network}</Badge>}
                      {series?.is_anime && <Badge tone="brand">Anime</Badge>}
                      {c.episode_order && c.episode_order !== 'aired' && <Badge>{c.episode_order} order</Badge>}
                    </span>
                  </span>
                  {selected ? (
                    <Check className="h-4 w-4 shrink-0 text-accent" aria-hidden />
                  ) : (
                    <ConfidenceMeter value={c.score} label="Score" />
                  )}
                </div>
                {selected && (
                  <div className="pointer-events-none mt-1">
                    <ConfidenceMeter value={c.score} label="Score" />
                  </div>
                )}
                {eps && <p className="pointer-events-none mt-1 truncate font-mono text-2xs text-ice-100/80">{eps}</p>}
                {c.reasons.length > 0 && (
                  <div className="relative z-10 mt-1">
                    <button
                      type="button"
                      className="text-2xs text-muted hover:text-ice-100"
                      aria-expanded={expanded === c.id}
                      onClick={() => setExpanded((x) => (x === c.id ? null : c.id))}
                    >
                      {expanded === c.id ? 'Hide reasons' : `Why ${Math.round(c.score * 100)}%?`}
                    </button>
                    {expanded === c.id && (
                      <ul className="mt-1 list-disc pl-4 text-2xs text-muted">
                        {c.reasons.map((r) => (
                          <li key={r}>{r}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
