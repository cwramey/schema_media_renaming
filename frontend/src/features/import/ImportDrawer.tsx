import { useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { ClipboardPaste, FolderOpen, Play, X } from 'lucide-react';
import { useCreateJob, useFormats, useSettings } from '@/api/hooks';
import type { MediaKind, RenameMode } from '@/api/types';
import { RENAME_MODES } from '@/api/types';
import { formatNumber } from '@/lib/format';
import { KIND_LABEL, MODE_INFO } from '@/lib/labels';
import { splitPath } from '@/lib/pathDiff';
import {
  Button,
  Combobox,
  Drawer,
  EmptyState,
  Field,
  IconButton,
  Input,
  Select,
  Switch,
  Textarea,
} from '@/components/ui';
import { FileBrowser } from './FileBrowser';

type KindChoice = MediaKind | 'auto';

/** Sources listed by path; the rest are counted (the list is not virtualized). */
const SOURCES_SHOWN = 200;

/**
 * The import picker as a drawer (side sheet on desktop, bottom sheet on phones), opened from the
 * top bar's "Import files" button or any URL with `?import=1`. Nothing is fetched while closed.
 */
export function ImportDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  return open ? <ImportDrawerOpen onClose={onClose} /> : null;
}

function Section({ icon, title, description, children }: { icon: ReactNode; title: string; description?: string; children: ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <header className="flex items-start gap-2">
        <span className="mt-0.5 text-muted [&>svg]:h-4 [&>svg]:w-4">{icon}</span>
        <div className="min-w-0">
          <h3 className="text-[13px] font-semibold text-ice-100">{title}</h3>
          {description && <p className="text-xs text-muted">{description}</p>}
        </div>
      </header>
      {children}
    </section>
  );
}

function ImportDrawerOpen({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const settings = useSettings();
  const formats = useFormats();
  const create = useCreateJob();

  const [selected, setSelected] = useState<string[]>([]);
  const [paste, setPaste] = useState('');
  const [kind, setKind] = useState<KindChoice>('auto');
  const [mode, setMode] = useState<RenameMode | 'default'>('default');
  const [formatId, setFormatId] = useState<string>('default');
  const [dest, setDest] = useState('');
  const [autoExecute, setAutoExecute] = useState(false);
  const [label, setLabel] = useState('');

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const toggle = (path: string, on: boolean) =>
    setSelected((cur) => (on ? (cur.includes(path) ? cur : [...cur, path]) : cur.filter((p) => p !== path)));
  /** "Select N videos": one update with set lookups (per-file toggles would be quadratic). */
  const toggleMany = (paths: string[], on: boolean) =>
    setSelected((cur) => {
      if (on) {
        const have = new Set(cur);
        const add = paths.filter((p) => !have.has(p));
        return add.length ? [...cur, ...add] : cur;
      }
      const drop = new Set(paths);
      return cur.filter((p) => !drop.has(p));
    });

  const addPasted = () => {
    const lines = paste
      .split(/\r?\n/)
      .map((l) => l.trim().replace(/^["']|["']$/g, ''))
      .filter(Boolean);
    setSelected((cur) => [...cur, ...lines.filter((l) => !cur.includes(l))]);
    setPaste('');
  };

  const defaultMode = settings.data?.default_mode ?? 'move';
  const effectiveMode = mode === 'default' ? defaultMode : mode;

  const formatOptions = useMemo(() => {
    const list = (formats.data ?? []).filter((f) => kind === 'auto' || kind === 'unknown' || f.kind === kind);
    return [
      { value: 'default', label: 'Default format for each kind' },
      ...list.map((f) => ({ value: String(f.id), label: `${f.name} (${KIND_LABEL[f.kind]})${f.is_default ? ' · default' : ''}` })),
    ];
  }, [formats.data, kind]);

  const destOptions = useMemo(() => {
    const roots = settings.data?.library_roots ?? {};
    return Object.entries(roots)
      .filter(([, v]) => !!v)
      .map(([k, v]) => ({ value: v as string, label: v as string, description: `${KIND_LABEL[k as MediaKind]} library` }));
  }, [settings.data]);

  const submit = () => {
    if (selected.length === 0) return;
    create.mutate(
      {
        paths: selected,
        kind: kind === 'auto' ? null : kind,
        mode: mode === 'default' ? null : mode,
        format_id: formatId === 'default' ? null : Number(formatId),
        dest_root: dest.trim() || null,
        auto_execute: autoExecute,
        label: label.trim() || null,
      },
      // Leaving for the job also drops `?import=1`, which closes the drawer.
      { onSuccess: (job) => navigate(`/jobs/${job.id}`) },
    );
  };

  return (
    <Drawer
      open
      onClose={onClose}
      title="Import files"
      width="min(44rem, 100vw)"
      footer={
        <Button
          variant="primary"
          size="lg"
          block
          leftIcon={<Play className="h-4 w-4" />}
          disabled={selected.length === 0}
          loading={create.isPending}
          onClick={submit}
        >
          {selected.length === 0 ? 'Select files or folders' : `Match ${selected.length} source${selected.length === 1 ? '' : 's'}`}
        </Button>
      }
    >
      <div className="flex flex-col gap-6 p-4">
        <p className="text-[13px] text-muted">
          Pick files or folders on the server. Schema matches them and shows you the new names before anything moves.
        </p>

        <Section icon={<FolderOpen />} title="Browse server" description="Folders are scanned recursively for videos.">
          <div className="overflow-hidden rounded-xl border border-ink-800">
            <FileBrowser selected={selectedSet} onToggle={toggle} onToggleMany={toggleMany} maxHeight="320px" />
          </div>
        </Section>

        <Section icon={<ClipboardPaste />} title="Paste paths" description="One file or folder per line, as the server sees it.">
          <Textarea
            mono
            rows={3}
            value={paste}
            onChange={(e) => setPaste(e.target.value)}
            placeholder={'/media/downloads/Never.After.Dark.2026.1080p.WEBRip.mkv\n/media/downloads/Season Pack/'}
            aria-label="Paths to import"
          />
          <div className="flex justify-end">
            <Button onClick={addPasted} disabled={!paste.trim()} className="h-10">
              Add paths
            </Button>
          </div>
        </Section>

        <section className="flex min-w-0 flex-col gap-2" aria-labelledby="import-sources">
          <div className="flex items-center justify-between gap-2">
            <h3 id="import-sources" className="text-[13px] font-semibold text-ice-100">
              Sources ({selected.length})
            </h3>
            {selected.length > 0 && (
              <Button size="sm" variant="subtle" onClick={() => setSelected([])}>
                Clear
              </Button>
            )}
          </div>
          <div className="rounded-xl border border-ink-800">
            {selected.length === 0 ? (
              <EmptyState compact title="Nothing selected" description="Tick files or folders, or paste paths." />
            ) : (
              <ul className="max-h-48 divide-y divide-ink-800 overflow-y-auto">
                {selected.slice(0, SOURCES_SHOWN).map((p) => {
                  const parts = splitPath(p);
                  return (
                    <li key={p} className="flex items-center gap-2 py-1 pl-4 pr-1">
                      <span className="min-w-0 flex-1 truncate font-mono text-xs" title={p}>
                        <span className="text-muted">{parts.folders.slice(-1)[0] ? `…/${parts.folders.slice(-1)[0]}/` : ''}</span>
                        {parts.ext ? `${parts.stem}.${parts.ext}` : `${parts.stem}/`}
                      </span>
                      <IconButton
                        label={`Remove ${p}`}
                        icon={<X className="h-3.5 w-3.5" />}
                        size="lg"
                        tooltip={false}
                        onClick={() => toggle(p, false)}
                      />
                    </li>
                  );
                })}
                {selected.length > SOURCES_SHOWN && (
                  <li className="py-1.5 pl-4 pr-1 text-xs text-muted">
                    and {formatNumber(selected.length - SOURCES_SHOWN)} more
                  </li>
                )}
              </ul>
            )}
          </div>
        </section>

        <section className="flex flex-col gap-4" aria-labelledby="import-options">
          <h3 id="import-options" className="text-[13px] font-semibold text-ice-100">
            Options
          </h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Media kind" hint="Auto lets Schema decide per file.">
              <Select<KindChoice>
                value={kind}
                onChange={setKind}
                options={[
                  { value: 'auto', label: 'Auto-detect' },
                  { value: 'movie', label: 'Movies' },
                  { value: 'series', label: 'TV' },
                  { value: 'anime', label: 'Anime' },
                ]}
              />
            </Field>
            <Field label="Rename mode" hint={MODE_INFO[effectiveMode].description}>
              <Select<RenameMode | 'default'>
                value={mode}
                onChange={setMode}
                options={[
                  { value: 'default', label: `Default (${MODE_INFO[defaultMode].label})` },
                  ...RENAME_MODES.map((m) => ({ value: m, label: MODE_INFO[m].label })),
                ]}
              />
            </Field>
            <Field label="Format">
              <Select value={formatId} onChange={setFormatId} options={formatOptions} />
            </Field>
            <Field label="Destination" hint="Leave empty to use the library folder for each kind." optional>
              <Combobox value={dest} onChange={setDest} options={destOptions} allowCustom mono placeholder="Library folder per kind" />
            </Field>
          </div>
          <Field label="Job label" optional>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Season pack from qBit" />
          </Field>
          <Field
            inline
            label="Rename automatically when sure"
            hint="Rename confident matches right after matching. Unsure files still wait for you."
          >
            <Switch checked={autoExecute} onChange={setAutoExecute} />
          </Field>
        </section>
      </div>
    </Drawer>
  );
}
