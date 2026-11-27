import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Eye, EyeOff, RefreshCw } from 'lucide-react';
import { api } from '@/api/client';
import { qk } from '@/api/keys';
import { useApiKey, useRotateApiKey, useSettings } from '@/api/hooks';
import type { ApiKeyOut, MediaKind } from '@/api/types';
import { Button, ConfirmDialog, Input } from '@/components/ui';
import { useCopy } from '@/lib/hooks';
import { useSaveSettings } from '../api';
import { PairsEditor, Row, RowSkeleton, SectionError, Segmented, SettingsSection } from '../controls';
import { KIND_PLURAL, MASKED_KEY, clientSnippet, keyFileContent, type DownloadClient } from '../mapping';

const CLIENTS: { value: DownloadClient; label: string }[] = [
  { value: 'qbittorrent', label: 'qBittorrent' },
  { value: 'sabnzbd', label: 'SABnzbd' },
  { value: 'generic', label: 'Other' },
];

const KIND_OPTIONS = (['movie', 'series', 'anime'] as const).map((k) => ({ value: k, label: KIND_PLURAL[k] }));

export function DownloadsSection() {
  const settings = useSettings();
  const save = useSaveSettings();
  const qc = useQueryClient();
  const [client, setClient] = useState<DownloadClient>('qbittorrent');
  const [reveal, setReveal] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const apiKey = useApiKey(reveal);
  const rotate = useRotateApiKey();
  const [copiedCmd, copyCmd] = useCopy();
  const [copiedKey, copyKey] = useCopy();
  const [copiedFile, copyFile] = useCopy();

  const origin = window.location.origin;
  const shownKey = reveal && apiKey.data ? apiKey.data.api_key : MASKED_KEY;
  const snippet = clientSnippet(client, origin, shownKey);

  /** The real key, fetched on demand even while it's hidden on screen. */
  const realKey = () => qc.fetchQuery({ queryKey: qk.apiKey, queryFn: () => api.get<ApiKeyOut>('/settings/api-key'), staleTime: Infinity });

  const copySnippet = async () => {
    const { api_key } = await realKey();
    await copyCmd(clientSnippet(client, origin, api_key).code);
  };

  const copyKeyFile = async () => {
    const { api_key } = await realKey();
    await copyFile(keyFileContent(api_key));
  };

  const codeBlock = 'min-w-0 flex-1 basis-full whitespace-pre-wrap sm:basis-auto rounded-lg border border-ink-800 bg-ink-950 px-3.5 py-3 font-mono text-xs leading-relaxed text-ice-100/90 [overflow-wrap:anywhere]';

  return (
    <SettingsSection
      id="downloads"
      num={6}
      title="Download clients"
      description="Optional. Lets your download app tell Schema the instant a download finishes, so there’s no wait at all."
    >
      <Row id="set-hook" group width="wide" label="Connect your download app" help={snippet.where}>
        <div className="flex flex-col gap-2.5">
          <Segmented
            labelledBy="set-hook-label"
            options={CLIENTS}
            value={client}
            onChange={setClient}
          />
          {snippet.keyFile && (
            <>
              <p id="set-hook-file-label" className="text-xs text-muted">
                {snippet.keyFile.where}
              </p>
              <div className="flex flex-wrap items-start gap-2 sm:flex-nowrap">
                <pre aria-labelledby="set-hook-file-label" className={codeBlock}>
                  {snippet.keyFile.content}
                </pre>
                <Button
                  variant="outline"
                  className="h-10"
                  aria-label="Copy the key file line"
                  leftIcon={copiedFile ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                  onClick={() => void copyKeyFile()}
                >
                  {copiedFile ? 'Copied' : 'Copy'}
                </Button>
              </div>
            </>
          )}
          <div className="flex flex-wrap items-start gap-2 sm:flex-nowrap">
            <pre id="set-hook" aria-labelledby="set-hook-label" className={codeBlock}>
              {snippet.code}
            </pre>
            <Button
              variant="primary"
              className="h-10"
              leftIcon={copiedCmd ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              onClick={() => void copySnippet()}
            >
              {copiedCmd ? 'Copied' : 'Copy'}
            </Button>
          </div>
          {!reveal && (
            <p className="text-xs text-muted">
              {snippet.keyFile
                ? 'Copy puts the real key in the key file line; it stays hidden here and out of the command.'
                : 'Copy puts the real key in the settings; it stays hidden here.'}
            </p>
          )}
        </div>
      </Row>

      <Row
        id="set-api-key"
        label="Secret key"
        help="Your download app uses this key to talk to Schema. Keep it private. Making a new key breaks the old one until you save the new key in your download app."
      >
        <div className="flex flex-col gap-2">
          <Input
            id="set-api-key"
            mono
            readOnly
            aria-describedby="set-api-key-help"
            className="[&_input]:h-10"
            value={reveal ? (apiKey.data?.api_key ?? (apiKey.isFetching ? 'Loading…' : '')) : '••••••••••••••••••••••••'}
          />
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              className="h-10"
              aria-pressed={reveal}
              leftIcon={reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              onClick={() => setReveal((r) => !r)}
            >
              {reveal ? 'Hide' : 'Show'}
            </Button>
            <Button
              variant="outline"
              className="h-10"
              leftIcon={copiedKey ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
              onClick={() => void realKey().then((k) => copyKey(k.api_key))}
            >
              {copiedKey ? 'Copied' : 'Copy'}
            </Button>
            <Button variant="danger" className="h-10" leftIcon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => setConfirmRotate(true)}>
              Make a new key
            </Button>
          </div>
        </div>
      </Row>

      {settings.isPending ? (
        <RowSkeleton rows={2} />
      ) : settings.isError || !settings.data ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="settings" />
      ) : (
        <>
          <Row
            id="set-dl-maps"
            group
            width="wide"
            label="Download app sees a different path"
            help="Only needed when the download app runs in its own container and reports paths Schema can’t see (for example /downloads instead of /media/downloads)."
          >
            <PairsEditor
              id="set-dl-maps"
              labelledBy="set-dl-maps-label"
              describedBy="set-dl-maps-help"
              leftHead="Path the download app reports"
              rightHead="Same folder for Schema"
              leftPlaceholder="/downloads"
              rightPlaceholder="/media/downloads"
              value={settings.data.path_mappings.map((m) => ({ a: m.source, b: m.target }))}
              onCommit={(rows) =>
                save((s) => ({ ...s, path_mappings: rows.map((r) => ({ source: r.a, target: r.b })) }), 'the download paths')
              }
            />
          </Row>
          <Row
            id="set-categories"
            group
            width="wide"
            label="Download categories"
            help="If your download app tags downloads with a category, tell Schema what each category holds so it doesn’t have to guess."
          >
            <PairsEditor
              id="set-categories"
              labelledBy="set-categories-label"
              describedBy="set-categories-help"
              leftHead="Category in download app"
              rightHead="Media type"
              leftPlaceholder="radarr"
              rightOptions={KIND_OPTIONS}
              defaultRight="movie"
              value={Object.entries(settings.data.category_kinds).map(([a, b]) => ({ a, b }))}
              onCommit={(rows) =>
                save(
                  (s) => ({
                    ...s,
                    category_kinds: Object.fromEntries(rows.map((r) => [r.a.toLowerCase(), r.b as MediaKind])),
                  }),
                  'the download categories',
                )
              }
            />
          </Row>
        </>
      )}

      <ConfirmDialog
        open={confirmRotate}
        onClose={() => setConfirmRotate(false)}
        onConfirm={() =>
          rotate.mutate(undefined, {
            onSuccess: () => {
              setReveal(true);
              setConfirmRotate(false);
            },
          })
        }
        loading={rotate.isPending}
        tone="danger"
        title="Make a new key?"
        description="The old key stops working right away. Save the new key in your download app afterwards (its key file or environment)."
        confirmLabel="Make a new key"
      />
    </SettingsSection>
  );
}
