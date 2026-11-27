import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, PencilRuler } from 'lucide-react';
import { useFormatCatalog, useFormats, useProviders, useSettings, useValidateFormat } from '@/api/hooks';
import type { FormatOut } from '@/api/types';
import { ButtonLink, Select, Skeleton, Switch } from '@/components/ui';
import { useSaveProviders, useSaveSetting, useSetDefaultFormat } from '../api';
import { Row, RowSkeleton, SectionError, SettingsSection } from '../controls';
import {
  EDIT_KINDS,
  LANGUAGES,
  NAMING_OPTIONS,
  defaultFormat,
  PREVIEW_SAMPLES,
  detectPreset,
  joinRoot,
  presetIdFor,
  type EditKind,
  type NamingChoice,
} from '../mapping';

const SELECT_COPY: Record<EditKind, { label: string; help: string }> = {
  movie: {
    label: 'Movies are named for',
    help: 'Each app expects a slightly different layout. Pick the one you watch with. "Custom" lets you write your own formula.',
  },
  series: {
    label: 'TV shows are named for',
    help: 'Controls the show folder, Season folders and episode names like S01E02.',
  },
  anime: {
    label: 'Anime is named for',
    help: '"Anime absolute" numbers episodes 1–500 instead of by season, which suits long-running series.',
  },
};

export function NamingSection() {
  const formats = useFormats();
  const catalog = useFormatCatalog();
  const settings = useSettings();
  const providers = useProviders();
  const saveSetting = useSaveSetting();
  const saveProviders = useSaveProviders();
  const setDefault = useSetDefaultFormat();
  const navigate = useNavigate();

  const presets = useMemo(() => catalog.data?.presets ?? [], [catalog.data]);
  const loading = formats.isPending || catalog.isPending;

  const choices = useMemo(() => {
    const out = {} as Record<EditKind, { choice: NamingChoice; format: FormatOut | undefined }>;
    for (const k of EDIT_KINDS) {
      const format = defaultFormat(formats.data, k);
      out[k] = { choice: detectPreset(format?.expression, k, presets), format };
    }
    return out;
  }, [formats.data, presets]);

  const pick = (kind: EditKind, choice: NamingChoice) => {
    if (choice === 'custom') {
      navigate(`/settings/formula?kind=${kind}`);
      return;
    }
    const preset = presets.find((p) => p.id === presetIdFor(choice, kind));
    if (!preset) return;
    const label = NAMING_OPTIONS[kind].find((o) => o.value === choice)?.label ?? preset.name;
    // Only overwrite the current default when it holds a preset; a custom formula is kept as its own format.
    setDefault(kind, preset.expression, label, choices[kind].choice !== 'custom');
  };

  const language = providers.data?.language ?? settings.data?.language ?? 'en';
  const langOptions = LANGUAGES.some((l) => l.code === language)
    ? LANGUAGES
    : [...LANGUAGES, { code: language, name: 'Other' }];

  const setLanguage = (code: string) => {
    void saveProviders({ language: code }, 'the title language');
    // AppSettings carries a copy of the language too; keep both in step.
    if (settings.data && settings.data.language !== code) saveSetting('language', code, 'the title language');
  };

  return (
    <SettingsSection
      id="naming"
      num={2}
      title="Naming"
      description="How renamed files and folders are laid out, so your media app recognises them."
    >
      {loading ? (
        <RowSkeleton rows={4} />
      ) : formats.isError ? (
        <SectionError error={formats.error} onRetry={() => void formats.refetch()} what="formats" />
      ) : (
        <>
          {EDIT_KINDS.map((k) => {
            const id = `set-naming-${k}`;
            const { choice, format } = choices[k];
            return (
              <Row
                key={k}
                id={id}
                label={SELECT_COPY[k].label}
                help={SELECT_COPY[k].help}
                aside={
                  choice === 'custom' && format ? (
                    <p className="mt-1 text-xs text-muted">
                      Using your formula <span className="font-medium text-ice-100">“{format.name}”</span>.
                    </p>
                  ) : null
                }
              >
                <Select<NamingChoice>
                  id={id}
                  aria-describedby={`${id}-help`}
                  className="[&_select]:h-10"
                  value={choice}
                  onChange={(v) => pick(k, v)}
                  options={NAMING_OPTIONS[k]}
                />
              </Row>
            );
          })}
          <PreviewRow roots={settings.data?.library_roots} formats={formats.data} />
        </>
      )}

      <Row
        id="set-formula"
        group
        label="Custom formula"
        help="For power users: write a FileBot-style naming formula with a live preview and validation. Most people never need this."
      >
        <ButtonLink
          to="/settings/formula"
          variant="outline"
          className="h-10"
          leftIcon={<PencilRuler className="h-3.5 w-3.5" />}
          aria-describedby="set-formula-help"
        >
          Open formula editor
        </ButtonLink>
      </Row>

      {providers.isPending && settings.isPending ? (
        <RowSkeleton rows={2} />
      ) : (
        <>
          <Row
            id="set-language"
            label="Title language"
            help='Titles are written in this language when the source has a translation. English keeps "Never After Dark" instead of the Japanese title.'
          >
            <Select
              id="set-language"
              aria-describedby="set-language-help"
              className="[&_select]:h-10"
              value={language}
              onChange={setLanguage}
              options={langOptions.map((l) => ({ value: l.code, label: `${l.name} (${l.code})` }))}
            />
          </Row>
          {settings.data && (
            <Row
              id="set-rename-in-place"
              width="auto"
              label="Rename the existing folder instead of making a new one"
              help="If a movie already sits in its own folder, Schema renames that folder in place. Faster, and keeps any extras you put there."
            >
              <Switch
                id="set-rename-in-place"
                aria-describedby="set-rename-in-place-help"
                checked={settings.data.rename_folders_in_place}
                onChange={(v) => saveSetting('rename_folders_in_place', v, 'the folder rename setting')}
              />
            </Row>
          )}
        </>
      )}
    </SettingsSection>
  );
}

/* ------------------------------------------------------------------ live preview */

function PreviewRow({
  roots,
  formats,
}: {
  roots: Partial<Record<string, string>> | undefined;
  formats: readonly FormatOut[] | undefined;
}) {
  return (
    <Row id="set-naming-preview" group label="What that looks like" help="A live preview using your current choices.">
      <div
        id="set-naming-preview"
        aria-labelledby="set-naming-preview-label"
        aria-live="polite"
        className="flex flex-col gap-3 rounded-lg border border-ink-800 bg-ink-950 px-3.5 py-3 font-mono text-xs leading-relaxed"
      >
        {EDIT_KINDS.map((k) => (
          <PreviewLine key={k} kind={k} expression={defaultFormat(formats, k)?.expression} root={roots?.[k]} />
        ))}
      </div>
    </Row>
  );
}

function PreviewLine({ kind, expression, root }: { kind: EditKind; expression: string | undefined; root: string | undefined }) {
  const sample = PREVIEW_SAMPLES[kind];
  const q = useValidateFormat(expression ? { expression, kind, samples: [sample] } : null);
  const out = q.data?.samples[0];
  return (
    <div className="min-w-0">
      <div className="break-all text-muted">{sample}</div>
      <div className="flex items-start gap-1.5 break-all text-ice-100">
        <ArrowRight className="mt-[3px] h-3 w-3 shrink-0 text-accent" aria-label="becomes" />
        {!expression ? (
          <span className="text-muted">No naming chosen yet</span>
        ) : q.isPending ? (
          <Skeleton className="mt-1 h-3 w-3/4" />
        ) : out?.output ? (
          <span className={q.isPlaceholderData ? 'opacity-60' : undefined}>
            {joinRoot(root, out.output)}
          </span>
        ) : (
          <span className="text-danger">{out?.error ?? q.data?.issues[0]?.message ?? 'Could not render a preview'}</span>
        )}
      </div>
    </div>
  );
}
