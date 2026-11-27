import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, ScanLine, Trash2 } from 'lucide-react';
import { useFormats, useScanWatchFolder, useSettings, useWatchFolders } from '@/api/hooks';
import type { AppSettings, MediaKind, RenameMode, WatchFolderOut } from '@/api/types';
import { Button, ConfirmDialog, PathText, Select, Switch, WatchStatusPill } from '@/components/ui';
import { MODE_INFO } from '@/lib/labels';
import { toast } from '@/lib/toast';
import { useSaveSettings, useWatchFolderActions } from '../api';
import {
  ChipsEditor,
  Row,
  RowSkeleton,
  SectionError,
  Segmented,
  SettingsSection,
  SubHeader,
  TextSetting,
} from '../controls';
import { KIND_PLURAL, checkAbsolutePath, isAbsolutePath, normalizePath, rowIds, selectedKey, settleOptions } from '../mapping';

const KIND_SEG: { value: 'auto' | Exclude<MediaKind, 'unknown'>; label: string }[] = [
  { value: 'auto', label: 'Auto-detect' },
  { value: 'movie', label: 'Movies' },
  { value: 'series', label: 'TV' },
  { value: 'anime', label: 'Anime' },
];

const checkPath = (v: string) => (isAbsolutePath(v) ? null : 'Use a full path, like /media/downloads.');

export function FoldersSection() {
  const settings = useSettings();
  const watch = useWatchFolders();
  const scan = useScanWatchFolder();
  const navigate = useNavigate();
  const save = useSaveSettings();
  const [adding, setAdding] = useState(false);

  const folders = watch.data ?? [];
  const many = folders.length > 1;

  const scanAll = () => {
    const targets = folders.filter((f) => f.enabled);
    for (const f of targets) {
      scan.mutate(f.id, {
        onSuccess: (job) =>
          toast.success('Scan started', {
            description: `Existing files in ${f.path} are being matched.`,
            action: { label: 'Open job', onClick: () => navigate(`/jobs/${job.id}`) },
          }),
      });
    }
  };

  const setRoot = (kind: Exclude<MediaKind, 'unknown'>, v: string, what: string) =>
    save((s) => {
      const library_roots = { ...s.library_roots };
      if (v) library_roots[kind] = normalizePath(v);
      else delete library_roots[kind];
      return { ...s, library_roots };
    }, what);

  return (
    <SettingsSection
      id="folders"
      num={1}
      title="Folders & watching"
      description="Where downloads arrive, how Schema notices them, and where finished files go."
    >
      {watch.isPending ? (
        <RowSkeleton rows={4} />
      ) : watch.isError ? (
        <SectionError error={watch.error} onRetry={() => void watch.refetch()} what="watch folders" />
      ) : (
        <>
          {folders.map((f, i) => (
            <WatchFolderBlock key={f.id} folder={f} index={i} many={many} />
          ))}
          {(folders.length === 0 || adding) && (
            <NewFolderBlock
              index={folders.length}
              many={folders.length > 0}
              onDone={() => setAdding(false)}
              cancellable={folders.length > 0}
            />
          )}
          <Row
            id="set-more-folders"
            group
            label="More watch folders"
            help='Each watch folder can have its own media type and wait time. Use "Scan now" to check a folder immediately instead of waiting.'
          >
            <div className="flex flex-wrap gap-2" role="group" aria-labelledby="set-more-folders-label">
              <Button
                variant="outline"
                className="h-10"
                leftIcon={<Plus className="h-3.5 w-3.5" />}
                disabled={adding || folders.length === 0}
                onClick={() => setAdding(true)}
              >
                Add watch folder
              </Button>
              <Button
                variant="outline"
                className="h-10"
                leftIcon={<ScanLine className="h-3.5 w-3.5" />}
                loading={scan.isPending}
                disabled={!folders.some((f) => f.enabled)}
                onClick={scanAll}
              >
                {many ? 'Scan all now' : 'Scan now'}
              </Button>
            </div>
          </Row>
        </>
      )}

      {settings.isPending ? (
        <RowSkeleton rows={4} />
      ) : settings.isError || !settings.data ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="settings" />
      ) : (
        <LibraryRows s={settings.data} setRoot={setRoot} onRoots={(v) => save((s) => ({ ...s, allowed_roots: v }), 'the allowed folders')} />
      )}
    </SettingsSection>
  );
}

function LibraryRows({
  s,
  setRoot,
  onRoots,
}: {
  s: AppSettings;
  setRoot: (kind: Exclude<MediaKind, 'unknown'>, v: string, what: string) => void;
  onRoots: (v: string[]) => void;
}) {
  const roots = rowIds('set-allowed-roots');
  return (
    <>
      <Row id="set-root-movie" label="Movies go to" help="Your Jellyfin Movies library folder. Each movie gets its own folder inside it.">
        <TextSetting
          id="set-root-movie"
          describedBy="set-root-movie-help"
          value={s.library_roots.movie ?? ''}
          placeholder="/media/movies"
          browse
          validate={checkPath}
          onCommit={(v) => setRoot('movie', v, 'the movies folder')}
        />
      </Row>
      <Row
        id="set-root-series"
        label="TV shows go to"
        help="Your Jellyfin Shows library folder. Shows are sorted into Season folders automatically."
      >
        <TextSetting
          id="set-root-series"
          describedBy="set-root-series-help"
          value={s.library_roots.series ?? ''}
          placeholder="/media/tv"
          browse
          validate={checkPath}
          onCommit={(v) => setRoot('series', v, 'the TV folder')}
        />
      </Row>
      <Row id="set-root-anime" label="Anime goes to" help="Kept separate so anime can use AniDB and absolute episode numbers.">
        <TextSetting
          id="set-root-anime"
          describedBy="set-root-anime-help"
          value={s.library_roots.anime ?? ''}
          placeholder="/media/anime"
          browse
          validate={checkPath}
          onCommit={(v) => setRoot('anime', v, 'the anime folder')}
        />
      </Row>
      <Row
        id={roots.id}
        group
        label="Folders Schema is allowed to touch"
        help="A safety fence. The file browser, watch folders and every move stay inside these folders and nowhere else."
      >
        <ChipsEditor
          id={roots.id}
          labelledBy={roots.labelId}
          describedBy={roots.helpId}
          value={s.allowed_roots}
          onChange={onRoots}
          normalize={normalizePath}
          check={checkAbsolutePath}
          split={false}
          placeholder="/media"
          noun="folder"
        />
      </Row>
    </>
  );
}

/* ------------------------------------------------------------------ one watch folder */

function WatchFolderBlock({ folder: f, index, many }: { folder: WatchFolderOut; index: number; many: boolean }) {
  const { update, remove } = useWatchFolderActions();
  const scan = useScanWatchFolder();
  const navigate = useNavigate();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const p = `set-wf${f.id}`;
  const settle = settleOptions(f.settle_seconds);

  return (
    <>
      <SubHeader
        actions={
          <>
            <WatchStatusPill status={f.enabled ? f.status : 'stopped'} />
            <Switch
              size="sm"
              checked={f.enabled}
              onChange={(enabled) => update(f.id, { enabled }, 'the watch folder')}
              label={<span className="text-xs text-muted">Watching</span>}
              aria-label={`Watch ${f.path}`}
            />
            {many && (
              <Button
                size="sm"
                variant="ghost"
                leftIcon={<ScanLine className="h-3.5 w-3.5" />}
                loading={scan.isPending}
                disabled={!f.enabled}
                onClick={() =>
                  scan.mutate(f.id, {
                    onSuccess: (job) =>
                      toast.success('Scan started', {
                        description: 'Existing files are being matched.',
                        action: { label: 'Open job', onClick: () => navigate(`/jobs/${job.id}`) },
                      }),
                  })
                }
              >
                Scan now
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              leftIcon={<Trash2 className="h-3.5 w-3.5" />}
              onClick={() => setConfirmRemove(true)}
            >
              Remove
            </Button>
          </>
        }
      >
        {many ? `Watch folder ${index + 1}` : 'Watch folder'}
        {f.last_error && <span className="ml-2 font-normal text-danger">{f.last_error}</span>}
      </SubHeader>
      <Row
        id={`${p}-path`}
        label="Watch this folder"
        help="New downloads land here. Schema checks it for finished video files. You can watch more than one folder."
      >
        <TextSetting
          id={`${p}-path`}
          describedBy={`${p}-path-help`}
          value={f.path}
          placeholder="/media/downloads"
          browse
          allowEmpty={false}
          validate={checkPath}
          onCommit={(path) => update(f.id, { path: normalizePath(path) }, 'the watch folder path')}
        />
      </Row>
      <Row
        id={`${p}-recursive`}
        width="auto"
        label="Look inside subfolders"
        help="Most downloads arrive inside their own folder (like the Never After Dark one). Leave this on."
      >
        <Switch
          id={`${p}-recursive`}
          aria-describedby={`${p}-recursive-help`}
          checked={f.recursive}
          onChange={(recursive) => update(f.id, { recursive }, 'the subfolder setting')}
        />
      </Row>
      <Row
        id={`${p}-settle`}
        group
        label="Wait after a file stops changing"
        help="Gives a download time to finish writing. Unfinished files (.part, .!qB) are always ignored, so a short wait is safe."
      >
        <Segmented
          id={`${p}-settle`}
          labelledBy={`${p}-settle-label`}
          describedBy={`${p}-settle-help`}
          options={settle.map((o) => ({ value: o.key, label: o.label }))}
          value={selectedKey(settle, f.settle_seconds)}
          onChange={(key) => {
            const opt = settle.find((o) => o.key === key);
            if (opt) update(f.id, { settle_seconds: opt.value }, 'the wait time');
          }}
        />
      </Row>
      <Row
        id={`${p}-kind`}
        group
        label="What goes in this folder"
        help="Auto-detect reads the file name to tell movies, TV and anime apart. Pick one if the folder only ever holds one type."
      >
        <Segmented
          id={`${p}-kind`}
          labelledBy={`${p}-kind-label`}
          describedBy={`${p}-kind-help`}
          options={KIND_SEG}
          value={f.kind && f.kind !== 'unknown' ? f.kind : 'auto'}
          onChange={(v) => update(f.id, { kind: v === 'auto' ? null : v }, 'the folder type')}
        />
      </Row>
      <FolderExtras folder={f} />
      <ConfirmDialog
        open={confirmRemove}
        onClose={() => setConfirmRemove(false)}
        onConfirm={() => {
          remove(f.id);
          setConfirmRemove(false);
        }}
        tone="danger"
        title="Stop watching this folder?"
        description="Files already in it are left alone. Jobs it created are kept."
        confirmLabel="Remove"
      >
        <PathText path={f.path} wrap />
      </ConfirmDialog>
    </>
  );
}

/** Per-folder overrides (mode, format, destination) folded away: most people never need them. */
function FolderExtras({ folder: f }: { folder: WatchFolderOut }) {
  const { update } = useWatchFolderActions();
  const formats = useFormats();
  const settings = useSettings();
  const p = `set-wf${f.id}`;
  const overrides = [f.mode, f.format_id, f.dest_root].filter((x) => x !== null && x !== '').length;
  return (
    <details className="group" open={overrides > 0 ? true : undefined}>
      <summary className="cursor-pointer list-none px-4 py-3 text-[13px] font-medium text-muted hover:text-ice-100 md:px-6 [&::-webkit-details-marker]:hidden">
        <span className="mr-1 inline-block transition-transform group-open:rotate-90" aria-hidden>
          ›
        </span>
        Other options for this folder{overrides > 0 ? ` (${overrides} changed)` : ''}
      </summary>
      <div className="divide-y divide-ink-800 border-t border-ink-800">
        <Row
          id={`${p}-mode`}
          label="What to do with files from this folder"
          help='Normally the choice under "Moving files" applies. Pick something else for just this folder, for example Copy for a folder you keep seeding from.'
        >
          <Select<RenameMode | 'default'>
            id={`${p}-mode`}
            aria-describedby={`${p}-mode-help`}
            className="[&_select]:h-10"
            value={f.mode ?? 'default'}
            onChange={(v) => update(f.id, { mode: v === 'default' ? null : v }, 'the folder’s move setting')}
            options={[
              { value: 'default', label: `Same as everywhere (${MODE_INFO[settings.data?.default_mode ?? 'move'].label})` },
              ...(Object.keys(MODE_INFO) as RenameMode[]).map((m) => ({ value: m, label: MODE_INFO[m].label })),
            ]}
          />
        </Row>
        <Row
          id={`${p}-format`}
          label="Naming for this folder"
          help='Normally each media type uses its choice under "Naming". Pick a saved format to use for everything from this folder.'
        >
          <Select
            id={`${p}-format`}
            aria-describedby={`${p}-format-help`}
            className="[&_select]:h-10"
            value={f.format_id === null ? 'default' : String(f.format_id)}
            onChange={(v) => update(f.id, { format_id: v === 'default' ? null : Number(v) }, 'the folder’s naming')}
            options={[
              { value: 'default', label: 'Same as everywhere' },
              ...(formats.data ?? [])
                .filter((fm) => fm.id > 0)
                .map((fm) => ({
                  value: String(fm.id),
                  label: `${fm.name}${fm.kind !== 'unknown' ? ` (${KIND_PLURAL[fm.kind]})` : ''}`,
                })),
            ]}
          />
        </Row>
        <Row
          id={`${p}-dest`}
          label="Send finished files somewhere else"
          help="Leave empty to use the Movies / TV / Anime folders below. Fill in a folder to send everything from this watch folder there instead."
        >
          <TextSetting
            id={`${p}-dest`}
            describedBy={`${p}-dest-help`}
            value={f.dest_root ?? ''}
            placeholder="Library folder per type"
            browse
            validate={checkPath}
            onCommit={(v) => update(f.id, { dest_root: v ? normalizePath(v) : null }, 'the folder’s destination')}
          />
        </Row>
      </div>
    </details>
  );
}

/** Draft block for a folder that doesn't exist yet: created once a path is entered. */
function NewFolderBlock({
  index,
  many,
  onDone,
  cancellable,
}: {
  index: number;
  many: boolean;
  onDone: () => void;
  cancellable: boolean;
}) {
  const { create } = useWatchFolderActions();
  const [busy, setBusy] = useState(false);
  const id = 'set-wf-new-path';
  return (
    <>
      <SubHeader
        actions={
          cancellable && (
            <Button size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          )
        }
      >
        {many ? `Watch folder ${index + 1} (new)` : 'Watch folder'}
      </SubHeader>
      <Row
        id={id}
        label="Watch this folder"
        help="New downloads land here. Schema checks it for finished video files. You can watch more than one folder."
      >
        <TextSetting
          id={id}
          describedBy={`${id}-help`}
          value=""
          placeholder={busy ? 'Adding…' : '/media/downloads'}
          browse
          validate={checkPath}
          onCommit={(path) => {
            if (!path) return;
            setBusy(true);
            void create({
              path: normalizePath(path),
              enabled: true,
              recursive: true,
              kind: null,
              auto_execute: true,
              settle_seconds: 30,
              format_id: null,
              dest_root: null,
              mode: null,
            }).then((saved) => {
              setBusy(false);
              if (saved) onDone();
            });
          }}
        />
      </Row>
    </>
  );
}
