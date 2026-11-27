import { useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PlugZap, Plus } from 'lucide-react';
import { api, errorMessage } from '@/api/client';
import { useProviders, useSettings, useTestProvider } from '@/api/hooks';
import type { MediaServerConfig, MediaServerKind, MediaServerTest, ProviderStatus, ProvidersOut } from '@/api/types';
import { SECRET_MASK } from '@/api/types';
import { Button, ConfirmDialog, Dialog, Field, Input } from '@/components/ui';
import { PROVIDER_CREDENTIAL_FIELDS, providerLabel } from '@/lib/labels';
import { useSaveProviders, useSaveSettings } from '../api';
import { ConnStatus, PairsEditor, Row, RowSkeleton, SectionError, SettingsSection, ToggleRow } from '../controls';
import { hostOf, providerConn } from '../mapping';

/* ================================================================== provider copy */

const PROVIDER_ORDER = ['tmdb', 'tvdb', 'anidb', 'trakt', 'tvmaze'];

function providerCopy(p: ProviderStatus): { label: string; help: string } {
  switch (p.name) {
    case 'tmdb':
      return {
        label: 'The Movie Database (TMDb)',
        help: p.configured
          ? 'Your main source for movies, TV and anime. Already set up, nothing to do.'
          : 'Your main source for movies, TV and anime. Needs a free API key (or read token) from themoviedb.org.',
      };
    case 'tvdb':
      return {
        label: 'TheTVDB',
        help: 'Optional. Better episode titles and air dates for TV. Needs a free API key from thetvdb.com (subscriber PIN only if you have one).',
      };
    case 'anidb':
      return {
        label: 'AniDB',
        help: 'Optional. The most accurate source for anime. Needs a client name and version you register at anidb.net.',
      };
    case 'trakt':
      return { label: 'Trakt', help: 'Optional backup for movies and TV. Needs a Trakt API client ID.' };
    case 'tvmaze':
      return { label: 'TVmaze', help: 'Free backup for TV. No key needed.' };
    case 'animelists':
      return {
        label: p.label || 'Anime-Lists',
        help: 'Translates anime episode numbers between AniDB, TheTVDB and TMDb. No key needed.',
      };
    default:
      return { label: p.label || providerLabel(p.name), help: p.requires_key ? 'Needs a key.' : 'No key needed.' };
  }
}

export function ConnectionsSection() {
  const providers = useProviders();
  const settings = useSettings();

  return (
    <SettingsSection
      id="connections"
      num={5}
      title="Connections"
      description="Where Schema looks up titles, and which media server it tells about new files."
    >
      {providers.isPending ? (
        <RowSkeleton rows={5} />
      ) : providers.isError || !providers.data ? (
        <SectionError error={providers.error} onRetry={() => void providers.refetch()} what="title sources" />
      ) : (
        <ProviderRows data={providers.data} />
      )}
      {settings.isPending ? (
        <RowSkeleton rows={3} />
      ) : settings.isError || !settings.data ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="media servers" />
      ) : (
        <MediaServerRows servers={settings.data.media_servers} />
      )}
    </SettingsSection>
  );
}

/* ================================================================== providers */

function ProviderRows({ data }: { data: ProvidersOut }) {
  const sorted = [...data.statuses].sort((a, b) => {
    const ia = PROVIDER_ORDER.indexOf(a.name);
    const ib = PROVIDER_ORDER.indexOf(b.name);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return (
    <>
      {sorted.map((p) => (
        <ProviderRow key={p.name} p={p} data={data} />
      ))}
    </>
  );
}

function ProviderRow({ p, data }: { p: ProviderStatus; data: ProvidersOut }) {
  const save = useSaveProviders();
  const test = useTestProvider();
  const [editing, setEditing] = useState(false);
  const off = data.disabled.includes(p.name) || !p.enabled;
  const conn = providerConn(p, off);
  const { label, help } = providerCopy(p);
  const fields = PROVIDER_CREDENTIAL_FIELDS[p.name] ?? [];
  const id = `set-provider-${p.name}`;
  const setOff = (turnOff: boolean) =>
    void save(
      { disabled: turnOff ? [...new Set([...data.disabled, p.name])] : data.disabled.filter((n) => n !== p.name) },
      `${providerLabel(p.name)} on/off`,
    );

  return (
    <Row id={id} group label={label} help={help}>
      <ConnStatus
        id={id}
        tone={conn.tone}
        status={conn.status}
        detail={
          <>
            {p.healthy === false && p.last_error && !off && <p className="text-xs text-danger">{p.last_error}</p>}
            {editing && (
              <CredentialsForm
                p={p}
                fields={fields}
                stored={data.credentials}
                onDone={() => setEditing(false)}
                onSaved={() => test.mutate(p.name)}
              />
            )}
          </>
        }
      >
        {fields.length > 0 && !editing && (
          <Button variant="outline" aria-expanded={editing} onClick={() => setEditing(true)}>
            {conn.needsSetup ? (p.name === 'anidb' ? 'Set up' : 'Add key') : p.name === 'anidb' ? 'Edit' : 'Change key'}
          </Button>
        )}
        {!conn.needsSetup && !off && (
          <Button variant="outline" leftIcon={<PlugZap className="h-3.5 w-3.5" />} loading={test.isPending} onClick={() => test.mutate(p.name)}>
            Test
          </Button>
        )}
        {!conn.needsSetup && (
          <Button variant="outline" onClick={() => setOff(!off)}>
            {off ? 'Turn on' : 'Turn off'}
          </Button>
        )}
      </ConnStatus>
    </Row>
  );
}

function CredentialsForm({
  p,
  fields,
  stored,
  onDone,
  onSaved,
}: {
  p: ProviderStatus;
  fields: { field: string; label: string; secret: boolean }[];
  stored: Record<string, string | null>;
  onDone: () => void;
  onSaved: () => void;
}) {
  const save = useSaveProviders();
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.field, stored[f.field] && stored[f.field] !== SECRET_MASK ? String(stored[f.field]) : ''])),
  );
  const [busy, setBusy] = useState(false);
  const hasStored = fields.some((f) => stored[f.field] !== null && stored[f.field] !== undefined);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const credentials: Record<string, string | null> = {};
    for (const f of fields) {
      const v = values[f.field].trim();
      const was = stored[f.field];
      if (f.secret) {
        // Blank secret = keep what's saved.
        if (v) credentials[f.field] = v;
      } else if (v !== (was ?? '')) {
        credentials[f.field] = v || null;
      }
    }
    if (Object.keys(credentials).length === 0) {
      onDone();
      return;
    }
    setBusy(true);
    const ok = await save({ credentials }, `the ${providerLabel(p.name)} key`);
    setBusy(false);
    if (ok) {
      onDone();
      onSaved();
    }
  };

  const clear = async () => {
    setBusy(true);
    const ok = await save(
      { credentials: Object.fromEntries(fields.map((f) => [f.field, null])) },
      `the ${providerLabel(p.name)} key`,
    );
    setBusy(false);
    if (ok) onDone();
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="mt-1 flex flex-col gap-3 rounded-lg border border-ink-800 bg-ink-950/50 p-3">
      {fields.map((f) => {
        const saved = stored[f.field] === SECRET_MASK;
        return (
          <Field key={f.field} label={f.label} hint={saved ? 'Saved. Leave empty to keep it, or type a new one.' : undefined}>
            <Input
              mono
              type={f.secret ? 'password' : f.field === 'anidb_client_version' ? 'number' : 'text'}
              autoComplete="off"
              spellCheck={false}
              className="[&_input]:h-10"
              value={values[f.field]}
              placeholder={saved ? '•••••••• (saved)' : ''}
              onChange={(e) => setValues((v) => ({ ...v, [f.field]: e.target.value }))}
            />
          </Field>
        );
      })}
      <p className="text-xs text-muted">
        Keys set with environment variables (like TMDB_API_KEY) take priority over what you save here.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" loading={busy}>
          Save
        </Button>
        <Button variant="ghost" onClick={onDone} disabled={busy}>
          Cancel
        </Button>
        {hasStored && (
          <Button variant="danger" className="ml-auto" onClick={() => void clear()} disabled={busy}>
            Remove key
          </Button>
        )}
      </div>
    </form>
  );
}

/* ================================================================== media servers */

const KIND_NAMES: Record<MediaServerKind, string> = { jellyfin: 'Jellyfin', plex: 'Plex', emby: 'Emby' };
const kindName = (k: string) => KIND_NAMES[k as MediaServerKind] ?? k;

function newId() {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID().replace(/-/g, '')
    : Math.random().toString(16).slice(2).padEnd(32, '0');
}

const newServer = (kind: MediaServerKind): MediaServerConfig => ({
  id: newId(),
  kind,
  name: KIND_NAMES[kind],
  url: '',
  token: '',
  enabled: true,
  path_mappings: [],
});

/** Status of a saved server, checked once when the page opens (and again on "Test"). */
function useServerCheck(s: MediaServerConfig) {
  return useQuery({
    queryKey: ['settings', 'media-server-check', s.id, s.url, s.token === SECRET_MASK ? 'saved' : 'unsaved'],
    queryFn: () => api.post<MediaServerTest>('/mediaservers/test', s),
    enabled: s.enabled && !!s.url && !!s.token,
    staleTime: 5 * 60_000,
    retry: false,
    meta: { silent: true },
  });
}

function MediaServerRows({ servers }: { servers: MediaServerConfig[] }) {
  const save = useSaveSettings();
  const [editing, setEditing] = useState<MediaServerConfig | null>(null);
  const hasJellyfin = servers.some((s) => s.kind === 'jellyfin');

  const upsert = (cfg: MediaServerConfig) => {
    save(
      (st) => ({
        ...st,
        media_servers: st.media_servers.some((x) => x.id === cfg.id)
          ? st.media_servers.map((x) => (x.id === cfg.id ? cfg : x))
          : [...st.media_servers, cfg],
      }),
      cfg.name,
    );
    setEditing(null);
  };

  return (
    <>
      {!hasJellyfin && (
        <Row
          id="set-ms-jellyfin-new"
          group
          label="Jellyfin"
          help="After each move, Schema tells Jellyfin to refresh so new files show up in seconds."
        >
          <ConnStatus id="set-ms-jellyfin-new" tone="neutral" status="Not set up · optional">
            <Button variant="outline" onClick={() => setEditing(newServer('jellyfin'))}>
              Set up
            </Button>
          </ConnStatus>
        </Row>
      )}
      {servers.map((s) => (
        <MediaServerBlock key={s.id} s={s} onEdit={() => setEditing(s)} />
      ))}
      <Row id="set-ms-add" group label="Another media server" help="Tell Plex or Emby about new files too.">
        <div className="flex flex-wrap gap-2" role="group" aria-labelledby="set-ms-add-label">
          <Button variant="outline" className="h-10" leftIcon={<Plus className="h-3.5 w-3.5" />} onClick={() => setEditing(newServer('plex'))}>
            Add Plex
          </Button>
          <Button variant="outline" className="h-10" leftIcon={<Plus className="h-3.5 w-3.5" />} onClick={() => setEditing(newServer('emby'))}>
            Add Emby
          </Button>
          {hasJellyfin && (
            <Button
              variant="outline"
              className="h-10"
              leftIcon={<Plus className="h-3.5 w-3.5" />}
              onClick={() => setEditing(newServer('jellyfin'))}
            >
              Add Jellyfin
            </Button>
          )}
        </div>
      </Row>
      {editing && (
        <ServerDialog
          server={editing}
          isNew={!servers.some((s) => s.id === editing.id)}
          stored={servers.find((s) => s.id === editing.id)}
          onClose={() => setEditing(null)}
          onSave={upsert}
        />
      )}
    </>
  );
}

function MediaServerBlock({ s, onEdit }: { s: MediaServerConfig; onEdit: () => void }) {
  const save = useSaveSettings();
  const check = useServerCheck(s);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const name = s.name || kindName(s.kind);
  const kind = kindName(s.kind);
  const p = `set-ms-${s.id}`;
  const host = hostOf(s.url);

  const patch = (changes: Partial<MediaServerConfig>, what: string) =>
    save((st) => ({ ...st, media_servers: st.media_servers.map((x) => (x.id === s.id ? { ...x, ...changes } : x)) }), what);

  let tone: 'neutral' | 'success' | 'danger' | 'info' = s.enabled ? 'info' : 'neutral';
  let status = s.enabled ? `Set up · ${host}` : `Refresh turned off · ${host}`;
  const failed = check.isError || (check.data && !check.data.ok);
  if (check.isFetching) {
    status = `Checking · ${host}`;
  } else if (check.data?.ok) {
    tone = 'success';
    status = `Connected · ${host}${s.enabled ? '' : ' · refresh off'}`;
  } else if (failed) {
    tone = 'danger';
    status = `Couldn’t connect · ${host}`;
  }
  const problem = check.isFetching
    ? null
    : check.data && !check.data.ok
      ? check.data.message
      : check.isError
        ? errorMessage(check.error)
        : null;

  return (
    <>
      <Row
        id={p}
        group
        label={name}
        help={`After each move, Schema tells ${kind} to refresh so new files show up in seconds.`}
      >
        <ConnStatus id={p} tone={tone} status={status} detail={problem && <p className="text-xs text-danger">{problem}</p>}>
          <Button variant="outline" onClick={onEdit}>
            Edit
          </Button>
          <Button
            variant="outline"
            leftIcon={<PlugZap className="h-3.5 w-3.5" />}
            loading={check.isFetching}
            onClick={() => void check.refetch()}
          >
            Test
          </Button>
          <Button variant="outline" onClick={() => setConfirmRemove(true)}>
            Remove
          </Button>
        </ConnStatus>
      </Row>
      <ToggleRow
        id={`${p}-enabled`}
        label={`Refresh ${name} after each move`}
        help={`Turn off if you’d rather ${kind} rescan on its own schedule.`}
        checked={s.enabled}
        onChange={(enabled) => patch({ enabled }, `the ${name} refresh setting`)}
      />
      <Row
        id={`${p}-maps`}
        group
        width="wide"
        label={`${name} sees files at a different path`}
        help={`Only needed if ${kind}’s container mounts your media somewhere else. Leave empty if both see the same folders.`}
      >
        <PairsEditor
          id={`${p}-maps`}
          labelledBy={`${p}-maps-label`}
          describedBy={`${p}-maps-help`}
          leftHead="Path in Schema"
          rightHead={`Path in ${kind}`}
          leftPlaceholder="/media/movies"
          rightPlaceholder="/data/movies"
          value={s.path_mappings.map((m) => ({ a: m.source, b: m.target }))}
          onCommit={(rows) => patch({ path_mappings: rows.map((r) => ({ source: r.a, target: r.b })) }, `the ${name} paths`)}
        />
      </Row>
      <ConfirmDialog
        open={confirmRemove}
        onClose={() => setConfirmRemove(false)}
        onConfirm={() => {
          save((st) => ({ ...st, media_servers: st.media_servers.filter((x) => x.id !== s.id) }), `removing ${name}`);
          setConfirmRemove(false);
        }}
        tone="danger"
        title={`Remove ${name}?`}
        description={`Schema stops telling ${kind} about new files. You can add it again any time.`}
        confirmLabel="Remove"
      />
    </>
  );
}

function ServerDialog({
  server,
  isNew,
  stored,
  onClose,
  onSave,
}: {
  server: MediaServerConfig;
  isNew: boolean;
  stored: MediaServerConfig | undefined;
  onClose: () => void;
  onSave: (s: MediaServerConfig) => void;
}) {
  const [form, setForm] = useState<MediaServerConfig>(server);
  const [token, setToken] = useState('');
  const [result, setResult] = useState<MediaServerTest | null>(null);
  const [testing, setTesting] = useState(false);
  const kind = kindName(form.kind);
  const url = form.url.trim().replace(/\/+$/, '');
  const urlOk = /^https?:\/\/.+/.test(url);
  const hasSaved = !isNew && stored?.token === SECRET_MASK;
  const urlChanged = !!stored && stored.url.replace(/\/+$/, '') !== url;
  const tokenNeeded = !token.trim() && (!hasSaved || urlChanged);
  const valid = !!form.name.trim() && urlOk && !tokenNeeded;
  const cfg = (): MediaServerConfig => ({ ...form, name: form.name.trim(), url, token: token.trim() || (hasSaved ? SECRET_MASK : '') });

  const runTest = async () => {
    setTesting(true);
    try {
      setResult(await api.post<MediaServerTest>('/mediaservers/test', cfg()));
    } catch (e) {
      setResult({ ok: false, message: errorMessage(e) });
    } finally {
      setTesting(false);
    }
  };

  const tokenLabel = form.kind === 'plex' ? 'Plex token' : 'API key';
  return (
    <Dialog
      open
      onClose={onClose}
      title={isNew ? `Set up ${kind}` : `Edit ${server.name}`}
      description={`Schema asks ${kind} to rescan only the folders it changed.`}
      footer={
        <>
          {result && (
            <span role="status" className={`mr-auto text-xs ${result.ok ? 'text-success' : 'text-danger'}`}>
              {result.ok ? 'Connected' : 'Couldn’t connect'}: {result.message}
            </span>
          )}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button leftIcon={<PlugZap className="h-3.5 w-3.5" />} loading={testing} disabled={!valid} onClick={() => void runTest()}>
            Test
          </Button>
          <Button variant="primary" disabled={!valid} onClick={() => onSave(cfg())}>
            {isNew ? 'Add' : 'Save'}
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) onSave(cfg());
        }}
      >
        <Field label="Name" hint="Shown in Settings and on Home.">
          <Input className="[&_input]:h-10" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} data-autofocus />
        </Field>
        <Field
          label="Address"
          hint={form.kind === 'plex' ? 'For example http://192.168.1.20:32400' : 'For example http://192.168.1.20:8096'}
          error={form.url && !urlOk ? 'Start with http:// or https://' : undefined}
        >
          <Input
            mono
            className="[&_input]:h-10"
            value={form.url}
            placeholder="http://"
            onChange={(e) => {
              setForm((f) => ({ ...f, url: e.target.value }));
              setResult(null);
            }}
          />
        </Field>
        <Field
          label={tokenLabel}
          hint={
            hasSaved && !urlChanged
              ? 'Saved. Leave empty to keep it, or type a new one.'
              : hasSaved && urlChanged
                ? `The address changed, so enter the ${tokenLabel.toLowerCase()} again.`
                : form.kind === 'plex'
                  ? 'Your X-Plex-Token.'
                  : `Create one in ${kind} under Dashboard → API Keys.`
          }
        >
          <Input
            mono
            type="password"
            autoComplete="off"
            className="[&_input]:h-10"
            value={token}
            placeholder={hasSaved ? '•••••••• (saved)' : ''}
            onChange={(e) => {
              setToken(e.target.value);
              setResult(null);
            }}
          />
        </Field>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}
