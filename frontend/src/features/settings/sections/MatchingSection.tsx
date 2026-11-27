import { useRef, useState, type DragEvent, type KeyboardEvent } from 'react';
import { ArrowDown, ArrowUp, GripVertical } from 'lucide-react';
import { useProviders, useSettings, useWatchFolders } from '@/api/hooks';
import type { AppSettings } from '@/api/types';
import { IconButton, PathText, Switch } from '@/components/ui';
import { cn } from '@/lib/cn';
import { providerLabel } from '@/lib/labels';
import { useSaveProviders, useSaveSetting, useWatchFolderActions } from '../api';
import { Row, RowSkeleton, SectionError, Segmented, SettingsSection } from '../controls';
import { EDIT_KINDS, KIND_PLURAL, confidenceOptions, moveItem, sameConfidence, selectedKey, type EditKind } from '../mapping';

export function MatchingSection() {
  const settings = useSettings();
  const saveSetting = useSaveSetting();

  return (
    <SettingsSection id="matching" num={3} title="Matching" description="How Schema works out which movie or episode a file is.">
      <AutoRenameRow />
      {settings.isPending ? (
        <RowSkeleton rows={2} />
      ) : settings.isError || !settings.data ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="settings" />
      ) : (
        <ConfidenceRow value={settings.data.min_confidence} onChange={(v) => saveSetting('min_confidence', v, 'how sure Schema must be')} />
      )}
      <ProviderOrderRow />
      {settings.data && <MatchingToggles s={settings.data} />}
    </SettingsSection>
  );
}

function AutoRenameRow() {
  const watch = useWatchFolders();
  const { update } = useWatchFolderActions();
  const folders = watch.data ?? [];
  const id = 'set-auto-execute';
  const label = 'Rename automatically when Schema is sure';
  const help = 'When on, confident matches are renamed with no clicks. When off, every file waits in "Needs review" on Home.';

  if (watch.isPending) return <RowSkeleton rows={1} />;
  if (folders.length <= 1) {
    const f = folders[0];
    return (
      <Row
        id={id}
        width="auto"
        label={label}
        help={help}
        aside={!f ? <p className="mt-1 text-xs text-muted">Add a watch folder first (section 1).</p> : null}
      >
        <Switch
          id={id}
          aria-describedby={`${id}-help`}
          checked={f?.auto_execute ?? false}
          disabled={!f}
          onChange={(v) => f && update(f.id, { auto_execute: v }, 'automatic renaming')}
        />
      </Row>
    );
  }
  return (
    <Row id={id} group label={label} help={`${help} Set it for each watch folder.`}>
      <ul id={id} aria-labelledby={`${id}-label`} className="flex flex-col gap-2">
        {folders.map((f) => (
          <li key={f.id} className="flex items-center justify-between gap-3 rounded-lg border border-ink-800 px-3 py-2">
            <PathText path={f.path} className="min-w-0 text-xs" />
            <Switch
              checked={f.auto_execute}
              aria-label={`Rename automatically from ${f.path}`}
              onChange={(v) => update(f.id, { auto_execute: v }, 'automatic renaming')}
            />
          </li>
        ))}
      </ul>
    </Row>
  );
}

function ConfidenceRow({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const opts = confidenceOptions(value);
  const id = 'set-confidence';
  return (
    <Row
      id={id}
      group
      label="How sure Schema must be"
      help="Below this, a file waits for you instead of being renamed. Balanced (85%) suits most libraries. Strict catches more mistakes but asks you more often."
    >
      <Segmented
        id={id}
        labelledBy={`${id}-label`}
        describedBy={`${id}-help`}
        options={opts.map((o) => ({ value: o.key, label: o.label }))}
        value={selectedKey(opts, value, sameConfidence)}
        onChange={(key) => {
          const o = opts.find((x) => x.key === key);
          if (o) onChange(o.value);
        }}
      />
    </Row>
  );
}

function MatchingToggles({ s }: { s: AppSettings }) {
  const saveSetting = useSaveSetting();
  const rows: { key: 'verify_crc' | 'probe_media' | 'skip_samples' | 'skip_extras'; label: string; help: string; what: string }[] = [
    {
      key: 'verify_crc',
      label: 'Check file checksums',
      help: 'Some files carry a code like [ABCD1234] in the name. Schema checks it; a mismatch usually means a damaged download, so it waits for you.',
      what: 'the checksum setting',
    },
    {
      key: 'probe_media',
      label: 'Read video details',
      help: 'Reads resolution, codec, HDR and audio (with ffprobe). Used in names like "- 1080p" and to decide whether a new copy is better quality.',
      what: 'the video details setting',
    },
    {
      key: 'skip_samples',
      label: 'Skip sample files',
      help: 'Short preview clips that come with some downloads are left alone.',
      what: 'the sample setting',
    },
    {
      key: 'skip_extras',
      label: 'Skip bonus clips & extras',
      help: 'Trailers, featurettes, behind-the-scenes and anime openings/endings are left alone instead of being mistaken for the movie.',
      what: 'the extras setting',
    },
  ];
  return (
    <>
      {rows.map((r) => {
        const id = `set-${r.key.replace(/_/g, '-')}`;
        return (
          <Row key={r.key} id={id} width="auto" label={r.label} help={r.help}>
            <Switch id={id} aria-describedby={`${id}-help`} checked={s[r.key]} onChange={(v) => saveSetting(r.key, v, r.what)} />
          </Row>
        );
      })}
    </>
  );
}

/* ------------------------------------------------------------------ provider lookup order */

function ProviderOrderRow() {
  const providers = useProviders();
  const save = useSaveProviders();
  const [announce, setAnnounce] = useState('');
  const id = 'set-provider-order';

  const onChange = (kind: EditKind, next: string[], moved: string) => {
    setAnnounce(`${providerLabel(moved)} is now number ${next.indexOf(moved) + 1} for ${KIND_PLURAL[kind]}.`);
    void save({ priority: { [kind]: next } }, 'the lookup order');
  };

  return (
    <Row
      id={id}
      group
      width="wide"
      label="Where titles come from"
      help="Schema asks the first source. If it can’t find a confident match, it tries the next one down. Drag to reorder."
    >
      {providers.isPending ? (
        <RowSkeleton rows={1} />
      ) : providers.isError || !providers.data ? (
        <SectionError error={providers.error} onRetry={() => void providers.refetch()} what="title sources" />
      ) : (
        <div id={id} role="group" aria-labelledby={`${id}-label`} aria-describedby={`${id}-help`}>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            {EDIT_KINDS.map((k) => (
              <OrderList
                key={k}
                kind={k}
                order={providers.data.priority[k] ?? []}
                disabled={providers.data.disabled}
                onChange={(next, moved) => onChange(k, next, moved)}
              />
            ))}
          </div>
          <p className="sr-only" aria-live="polite">
            {announce}
          </p>
        </div>
      )}
    </Row>
  );
}

/** Drag-ordered list; Alt+↑/↓ (or the arrow buttons) reorder from the keyboard. */
function OrderList({
  kind,
  order,
  disabled,
  onChange,
}: {
  kind: EditKind;
  order: string[];
  disabled: string[];
  onChange: (next: string[], moved: string) => void;
}) {
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const items = useRef<(HTMLLIElement | null)[]>([]);
  const headId = `set-order-${kind}`;

  const move = (from: number, to: number, refocus = false) => {
    if (to < 0 || to >= order.length || from === to) return;
    const next = moveItem(order, from, to);
    onChange(next, order[from]);
    if (refocus) requestAnimationFrame(() => items.current[to]?.focus());
  };

  const onKey = (e: KeyboardEvent<HTMLLIElement>, i: number) => {
    if (!(e.altKey || e.ctrlKey || e.metaKey)) return;
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      move(i, i - 1, true);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      move(i, i + 1, true);
    }
  };

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <h3 id={headId} className="text-[13px] font-semibold text-ice-100/90">
        {KIND_PLURAL[kind]}
      </h3>
      {order.length === 0 ? (
        <p className="text-xs text-muted">No sources for this type.</p>
      ) : (
        <ol className="flex flex-col gap-1.5" aria-labelledby={headId}>
          {order.map((name, i) => {
            const off = disabled.includes(name);
            return (
              <li
                key={name}
                ref={(el) => {
                  items.current[i] = el;
                }}
                tabIndex={0}
                draggable
                aria-label={`${providerLabel(name)}, number ${i + 1} of ${order.length}${off ? ', turned off' : ''}. Alt plus arrow keys to move.`}
                onKeyDown={(e) => onKey(e, i)}
                onDragStart={(e: DragEvent) => {
                  setDragging(i);
                  e.dataTransfer.effectAllowed = 'move';
                  e.dataTransfer.setData('text/plain', name);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  setOver(i);
                }}
                onDragLeave={() => setOver((o) => (o === i ? null : o))}
                onDrop={(e) => {
                  e.preventDefault();
                  if (dragging !== null) move(dragging, i);
                  setDragging(null);
                  setOver(null);
                }}
                onDragEnd={() => {
                  setDragging(null);
                  setOver(null);
                }}
                className={cn(
                  'flex min-h-10 cursor-grab items-center gap-2 rounded-lg border bg-ink-950 pl-2.5 pr-1 text-sm transition-colors active:cursor-grabbing',
                  over === i && dragging !== null && dragging !== i ? 'border-accent' : 'border-ink-700',
                  dragging === i && 'opacity-50',
                  off && 'text-muted',
                )}
              >
                <span className="tabular w-3.5 text-xs text-muted" aria-hidden>
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {providerLabel(name)}
                  {off && <span className="ml-1.5 text-2xs">(off)</span>}
                </span>
                <IconButton
                  label={`Move ${providerLabel(name)} up`}
                  icon={<ArrowUp className="h-3 w-3" />}
                  size="xs"
                  tooltip={false}
                  disabled={i === 0}
                  onClick={() => move(i, i - 1)}
                />
                <IconButton
                  label={`Move ${providerLabel(name)} down`}
                  icon={<ArrowDown className="h-3 w-3" />}
                  size="xs"
                  tooltip={false}
                  disabled={i === order.length - 1}
                  onClick={() => move(i, i + 1)}
                />
                <GripVertical className="h-4 w-4 shrink-0 text-muted" aria-hidden />
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
