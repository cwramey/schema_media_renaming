import { memo, useMemo, type MouseEvent } from 'react';
import { AlertTriangle, ArrowRight, Check, X } from 'lucide-react';
import type { JobItemOut, RenameMode } from '@/api/types';
import { cn } from '@/lib/cn';
import { episodeLabel } from '@/lib/format';
import { ITEM_ACTION, KIND_LABEL, type ItemAction } from '@/lib/labels';
import { Badge, Checkbox, ConfidenceMeter, FileName, ItemStatusPill, PathText } from '@/components/ui';
import { crcState, displayPathUnder, itemAction, itemActionTitle } from './model';

/** Chips summarizing what the parser recovered (kind, S/E or abs, year, vf, source, group, CRC). */
export function ParsedChips({ item, wrap }: { item: JobItemOut; wrap?: boolean }) {
  const p = item.parsed;
  const ep = episodeLabel(p);
  const crc = crcState(item);
  return (
    <span className={cn('flex min-w-0 items-center gap-1', wrap ? 'flex-wrap' : 'overflow-hidden')}>
      <Badge tone={p.kind === 'anime' ? 'brand' : p.kind === 'unknown' ? 'warning' : 'neutral'}>{KIND_LABEL[p.kind]}</Badge>
      {ep && (
        <Badge mono tone="info">
          {ep}
        </Badge>
      )}
      {p.year && <Badge mono>{p.year}</Badge>}
      {p.quality.resolution && <Badge mono>{p.quality.resolution}</Badge>}
      {p.quality.source && <Badge>{p.quality.remux ? `${p.quality.source} Remux` : p.quality.source}</Badge>}
      {p.edition && <Badge tone="brand">{p.edition}</Badge>}
      {p.part !== null && <Badge mono>pt{p.part}</Badge>}
      {p.version !== null && <Badge mono>v{p.version}</Badge>}
      {p.release_group && (
        <Badge mono className="max-w-[9rem] truncate" title={`Release group ${p.release_group}`}>
          {p.release_group}
        </Badge>
      )}
      {crc && (
        <Badge
          mono
          tone={crc === 'ok' ? 'success' : crc === 'mismatch' ? 'danger' : 'neutral'}
          title={`CRC32 ${p.crc32}: ${crc === 'ok' ? 'verified' : crc === 'mismatch' ? 'does not match the file' : 'not checked yet'}`}
          icon={crc === 'ok' ? <Check className="h-2.5 w-2.5" /> : crc === 'mismatch' ? <X className="h-2.5 w-2.5" /> : undefined}
        >
          CRC
        </Badge>
      )}
      {p.is_sample && <Badge tone="warning">Sample</Badge>}
      {p.is_extra && <Badge tone="warning">Extra</Badge>}
    </span>
  );
}

/** "Folder renamed" / "Moved" chip in front of the destination. */
function ActionBadge({ item, action }: { item: JobItemOut; action: ItemAction | null }) {
  if (!action) return null;
  return (
    <Badge tone={ITEM_ACTION[action].tone} title={itemActionTitle(item, action)}>
      {ITEM_ACTION[action].label}
    </Badge>
  );
}

export interface RowProps {
  item: JobItemOut;
  index: number;
  /** Roots the destination is shown relative to (`displayRoots`); keep the array stable. */
  roots: readonly string[];
  /** The job's rename mode (for the "Moved" / "Folder renamed" chip). */
  mode: RenameMode;
  active: boolean;
  selected: boolean;
  onActivate: (index: number, e: MouseEvent) => void;
  onToggleSelect: (index: number, on: boolean, shift: boolean) => void;
}

const noop = () => undefined;

/** Destination and action chip for a row, worked out only when the row's item changes. */
function useRowDestination(item: JobItemOut, roots: readonly string[], mode: RenameMode) {
  const path = useMemo(() => displayPathUnder(item, roots), [item, roots]);
  return { path, action: itemAction(item, mode) };
}

export const GRID_COLS =
  'grid-cols-[28px_88px_minmax(0,1fr)_14px_minmax(0,0.9fr)_14px_minmax(0,1.35fr)_80px]';

/** Desktop row: Original filename → Parsed metadata → Proposed path (SPEC §9.3). 32 px tall. */
export const ItemRow = memo(function ItemRow({ item, index, roots, mode, active, selected, onActivate, onToggleSelect }: RowProps) {
  const { path, action } = useRowDestination(item, roots, mode);
  return (
    <div
      id={`item-row-${item.id}`}
      role="row"
      aria-rowindex={index + 2}
      aria-selected={selected}
      onClick={(e) => onActivate(index, e)}
      className={cn(
        'grid h-8 cursor-pointer items-center gap-2 border-b border-ink-800/60 px-3 text-xs',
        GRID_COLS,
        active ? 'bg-ink-850 shadow-[inset_2px_0_0_0_#1DC6EF]' : 'hover:bg-ink-850/50',
        selected && !active && 'bg-brand-sky/[0.06]',
      )}
    >
      <span
        role="gridcell"
        onClick={(e) => {
          e.stopPropagation();
          // Handle here (not onChange) so shift-click range selection can read e.shiftKey.
          if ((e.target as HTMLElement).tagName === 'INPUT') onToggleSelect(index, !selected, e.shiftKey);
        }}
      >
        <Checkbox checked={selected} onChange={noop} aria-label={`Select ${item.filename}`} tabIndex={-1} />
      </span>
      <span role="gridcell" className="flex min-w-0 items-center">
        <ItemStatusPill status={item.status} />
      </span>
      <span role="gridcell" className="min-w-0">
        <FileName name={item.filename} className="text-ice-100/90" />
      </span>
      <ArrowRight className="h-3 w-3 text-muted/50" aria-hidden />
      <span role="gridcell" className="min-w-0">
        <ParsedChips item={item} />
      </span>
      <ArrowRight className="h-3 w-3 text-muted/50" aria-hidden />
      <span role="gridcell" className="flex min-w-0 items-center gap-1.5">
        {item.warnings.length > 0 && (
          <AlertTriangle className="h-3 w-3 shrink-0 text-warning" aria-label={`${item.warnings.length} warnings`} />
        )}
        {item.error ? (
          <span className="truncate text-danger" title={item.error}>
            {item.error}
          </span>
        ) : (
          <>
            <ActionBadge item={item} action={action} />
            <PathText path={path} compareTo={item.filename} placeholder="No destination yet" />
          </>
        )}
      </span>
      <span role="gridcell" className="flex justify-end">
        <ConfidenceMeter value={item.confidence} />
      </span>
    </div>
  );
});

/** Phone card: the three columns stacked. */
export const ItemCard = memo(function ItemCard({ item, index, roots, mode, active, selected, onActivate, onToggleSelect }: RowProps) {
  const { path, action } = useRowDestination(item, roots, mode);
  return (
    <div
      id={`item-row-${item.id}`}
      role="row"
      aria-rowindex={index + 2}
      aria-selected={selected}
      onClick={(e) => onActivate(index, e)}
      className={cn(
        'flex cursor-pointer gap-3 border-b border-ink-800 px-4 py-3',
        active ? 'bg-ink-850' : 'active:bg-ink-850',
        selected && 'bg-brand-sky/[0.06]',
      )}
    >
      <span role="gridcell" className="pt-0.5" onClick={(e) => e.stopPropagation()}>
        <Checkbox checked={selected} onChange={(on) => onToggleSelect(index, on, false)} aria-label={`Select ${item.filename}`} />
      </span>
      <div role="gridcell" className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <ItemStatusPill status={item.status} />
          <ConfidenceMeter value={item.confidence} />
        </div>
        <FileName name={item.filename} className="text-muted" />
        <ParsedChips item={item} wrap />
        {item.error ? (
          <p className="text-xs text-danger">{item.error}</p>
        ) : (
          <div className="flex min-w-0 flex-col items-start gap-1">
            <ActionBadge item={item} action={action} />
            <PathText path={path} compareTo={item.filename} wrap placeholder="No destination yet" />
          </div>
        )}
      </div>
    </div>
  );
});
