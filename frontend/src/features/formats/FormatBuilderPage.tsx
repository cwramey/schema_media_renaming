import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Copy,
  FileInput,
  FlaskConical,
  Loader2,
  Plus,
  Save,
  Star,
  Trash2,
} from 'lucide-react';
import {
  useCreateFormat,
  useDeleteFormat,
  useFormatCatalog,
  useFormats,
  usePreview,
  useUpdateFormat,
  useValidateFormat,
} from '@/api/hooks';
import type { FormatIssue, FormatOut, MediaKind } from '@/api/types';
import { cn } from '@/lib/cn';
import { useDebounced } from '@/lib/hooks';
import { KIND_LABEL } from '@/lib/labels';
import { lexicalIssues, tokenize, usedBindings, type IssueRange } from '@/lib/expr/tokenizer';
import {
  Badge,
  Button,
  Card,
  ConfidenceMeter,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Menu,
  PathText,
  Select,
  Skeleton,
  SkeletonRows,
  Switch,
  Tabs,
  Textarea,
} from '@/components/ui';
import { Page } from '@/components/layout/Page';
import { ExpressionEditor, type ExpressionEditorHandle } from './ExpressionEditor';
import { BindingPalette, MethodChips, PresetGallery } from './Palette';

type EditKind = Exclude<MediaKind, 'unknown'>;
const KINDS: EditKind[] = ['movie', 'series', 'anime'];

const STARTER: Record<EditKind, string> = {
  movie: "{ny}/{ny}{' - ' + edition}{' - pt' + pi}",
  series: "{n} ({y})/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}",
  anime: "{n}/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}",
};

interface Draft {
  id: number | null;
  name: string;
  kind: EditKind;
  expression: string;
  builtin: boolean;
  is_default: boolean;
}

const fromFormat = (f: FormatOut): Draft => ({
  id: f.id,
  name: f.name,
  kind: (f.kind === 'unknown' ? 'series' : f.kind) as EditKind,
  expression: f.expression,
  builtin: f.builtin,
  is_default: f.is_default,
});

/** Line:column for an offset (1-based), for issue lists. */
function lineCol(text: string, offset: number) {
  const before = text.slice(0, offset);
  const line = before.split('\n').length;
  const col = offset - before.lastIndexOf('\n');
  return { line, col };
}

export default function FormatBuilderPage() {
  const formats = useFormats();
  const catalog = useFormatCatalog();
  const create = useCreateFormat();
  const updateF = useUpdateFormat();
  const del = useDeleteFormat();
  const preview = usePreview();
  const editor = useRef<ExpressionEditorHandle>(null);
  const editorId = useId();
  // Settings → Naming opens the editor on the kind it came from (?kind=movie|series|anime).
  const [params] = useSearchParams();
  const kindParam = params.get('kind');
  const startKind: EditKind = KINDS.includes(kindParam as EditKind) ? (kindParam as EditKind) : 'series';

  const [draft, setDraft] = useState<Draft>({
    id: null,
    name: 'Untitled format',
    kind: startKind,
    expression: STARTER[startKind],
    builtin: false,
    is_default: false,
  });
  const [loadedFrom, setLoadedFrom] = useState<Draft | null>(null);
  const [caret, setCaret] = useState(0);
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [saveDefault, setSaveDefault] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [importText, setImportText] = useState('');
  const [importedNote, setImportedNote] = useState(false);
  const [tryNames, setTryNames] = useState('');

  // Load the default series format once formats arrive.
  const initialized = useRef(false);
  useEffect(() => {
    if (initialized.current || !formats.data) return;
    initialized.current = true;
    const first =
      formats.data.find((f) => f.kind === startKind && f.is_default) ??
      formats.data.find((f) => f.is_default) ??
      formats.data[0];
    if (first) {
      const d = fromFormat(first);
      setDraft(d);
      setLoadedFrom(d);
    }
  }, [formats.data, startKind]);

  const dirty =
    !loadedFrom || loadedFrom.expression !== draft.expression || loadedFrom.name !== draft.name || loadedFrom.kind !== draft.kind;

  /* ------------------------------------------------------------------ validation */
  const debounced = useDebounced(draft.expression, 250);
  const validate = useValidateFormat(debounced.trim() ? { expression: debounced, kind: draft.kind } : null);
  const tokens = useMemo(() => tokenize(draft.expression), [draft.expression]);
  const used = useMemo(() => usedBindings(tokens), [tokens]);
  const fresh = debounced === draft.expression && !validate.isPlaceholderData && !!validate.data;
  const serverIssues: FormatIssue[] = fresh ? (validate.data?.issues ?? []) : [];
  const localIssues: IssueRange[] = lexicalIssues(tokens).map((i) => ({ ...i, severity: 'error' as const }));
  const issues: IssueRange[] = [...serverIssues];
  for (const l of localIssues) {
    if (!issues.some((m) => m.start === l.start && m.end === l.end)) issues.push(l);
  }
  issues.sort((a, b) => a.start - b.start);
  const errors = issues.filter((i) => i.severity === 'error');
  const warnings = issues.filter((i) => i.severity === 'warning');
  const caretIssue = issues.find((i) => caret >= i.start && caret <= Math.max(i.end, i.start + 1));
  const validating = validate.isFetching || debounced !== draft.expression;

  /* ------------------------------------------------------------------ actions */
  const setExpression = (expression: string) => {
    setDraft((d) => ({ ...d, expression }));
    setImportedNote(false);
  };

  const loadFormat = (f: FormatOut) => {
    const d = fromFormat(f);
    setDraft(d);
    setLoadedFrom(d);
    setImportedNote(false);
  };

  const newFormat = () => {
    setDraft({ id: null, name: 'Untitled format', kind: draft.kind, expression: STARTER[draft.kind], builtin: false, is_default: false });
    setLoadedFrom(null);
  };

  const canSave = errors.length === 0 && draft.expression.trim().length > 0;

  const save = () => {
    if (!canSave) return;
    if (draft.id !== null && !draft.builtin) {
      updateF.mutate(
        { id: draft.id, body: { name: draft.name, kind: draft.kind, expression: draft.expression, is_default: draft.is_default } },
        { onSuccess: (f) => f && loadFormat(f) },
      );
    } else {
      openSaveAs();
    }
  };

  const openSaveAs = () => {
    setSaveName(draft.builtin || draft.id === null ? `${draft.name}${draft.builtin ? ' (copy)' : ''}` : `${draft.name} (copy)`);
    setSaveDefault(false);
    setSaveOpen(true);
  };

  const doSaveAs = () => {
    create.mutate(
      { name: saveName.trim() || 'Untitled format', kind: draft.kind, expression: draft.expression, is_default: saveDefault },
      {
        onSuccess: (f) => {
          setSaveOpen(false);
          if (f) loadFormat(f);
        },
      },
    );
  };

  const setDefault = () => {
    if (draft.id === null || draft.builtin) return;
    updateF.mutate(
      { id: draft.id, body: { name: draft.name, kind: draft.kind, expression: draft.expression, is_default: true } },
      { onSuccess: (f) => f && loadFormat(f) },
    );
  };

  const runPreview = () => {
    const names = tryNames
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean)
      .slice(0, 200);
    if (names.length === 0) return;
    preview.mutate({ names, kind: draft.kind, expression: draft.expression });
  };

  const groupedFormats = useMemo(() => {
    const g: Record<EditKind, FormatOut[]> = { movie: [], series: [], anime: [] };
    for (const f of formats.data ?? []) if (f.kind !== 'unknown') g[f.kind].push(f);
    return g;
  }, [formats.data]);

  /* ------------------------------------------------------------------ render */
  return (
    <>
    <div className="mx-auto w-full max-w-[1600px] px-4 pt-4 md:px-6 md:pt-5">
      <Link
        to="/settings#naming"
        className="inline-flex items-center gap-1.5 rounded-md text-[13px] font-medium text-muted hover:text-ice-100"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        Back to Settings
      </Link>
    </div>
    <Page
      className="pt-2 md:pt-3"
      title="Format builder"
      description="FileBot-compatible naming formats with live validation. Paste an existing FileBot format to import it."
      width="wide"
      actions={
        <Button leftIcon={<Plus className="h-4 w-4" />} onClick={newFormat}>
          New format
        </Button>
      }
    >
      <div className="grid gap-4 xl:grid-cols-[240px_minmax(0,1fr)_300px] lg:grid-cols-[220px_minmax(0,1fr)]">
        {/* -------------------------------------------- saved formats */}
        <Card title="Saved formats" flush className="lg:self-start">
          {formats.isPending ? (
            <SkeletonRows rows={5} className="p-3" />
          ) : formats.isError ? (
            <ErrorState compact error={formats.error} onRetry={() => void formats.refetch()} />
          ) : (
            <div className="flex max-h-[60vh] flex-col overflow-y-auto py-1">
              {KINDS.map((k) => (
                <div key={k} className="py-1">
                  <h3 className="px-3 py-1 text-2xs font-semibold uppercase tracking-wider text-muted">{KIND_LABEL[k]}</h3>
                  {groupedFormats[k].length === 0 && <p className="px-3 py-1 text-xs text-muted/80">None yet</p>}
                  {groupedFormats[k].map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => loadFormat(f)}
                      aria-current={draft.id === f.id ? 'true' : undefined}
                      className={cn(
                        'flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] transition-colors',
                        draft.id === f.id ? 'bg-ink-850 text-ice-100' : 'text-muted hover:bg-ink-850/60 hover:text-ice-100',
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{f.name}</span>
                      {f.is_default && <Star className="h-3 w-3 shrink-0 fill-current text-warning" aria-label="Default" />}
                      {f.builtin && <Badge>preset</Badge>}
                    </button>
                  ))}
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* -------------------------------------------- editor column */}
        <div className="flex min-w-0 flex-col gap-4">
          <Card flush>
            <div className="flex flex-wrap items-center gap-3 border-b border-ink-800 px-4 py-3">
              <Input
                value={draft.name}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                aria-label="Format name"
                className="min-w-[160px] flex-1"
                disabled={draft.builtin}
              />
              <Tabs<EditKind>
                variant="pill"
                value={draft.kind}
                onChange={(k) => setDraft((d) => ({ ...d, kind: k }))}
                items={KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))}
                aria-label="Media kind"
              />
              <div className="flex items-center gap-1.5">
                {draft.is_default && !dirty && (
                  <Badge tone="warning" icon={<Star className="h-2.5 w-2.5 fill-current" />}>
                    Default
                  </Badge>
                )}
                {dirty && <Badge tone="info">Unsaved</Badge>}
                <Button
                  variant="primary"
                  size="sm"
                  leftIcon={<Save className="h-3.5 w-3.5" />}
                  disabled={!canSave || (!dirty && draft.id !== null)}
                  loading={updateF.isPending}
                  onClick={save}
                  title={errors.length > 0 ? 'Fix the errors before saving' : undefined}
                >
                  {draft.builtin ? 'Save copy' : 'Save'}
                </Button>
                <Menu
                  label="Format actions"
                  items={[
                    { label: 'Save as new…', icon: <Copy />, onSelect: openSaveAs, disabled: !canSave },
                    {
                      label: `Set as default for ${KIND_LABEL[draft.kind].toLowerCase()}`,
                      icon: <Star />,
                      onSelect: setDefault,
                      disabled: draft.id === null || draft.builtin || draft.is_default || dirty,
                    },
                    {
                      label: 'Delete',
                      icon: <Trash2 />,
                      danger: true,
                      onSelect: () => setConfirmDelete(true),
                      disabled: draft.id === null || draft.builtin,
                    },
                  ]}
                />
              </div>
            </div>
            <div className="flex flex-col gap-3 p-4">
              {draft.builtin && (
                <p className="rounded-md bg-ink-850 px-3 py-2 text-xs text-muted">
                  Built-in presets are read-only. Edits are saved as a new format.
                </p>
              )}
              <label htmlFor={editorId} className="sr-only">
                Format expression
              </label>
              <ExpressionEditor
                ref={editor}
                id={editorId}
                value={draft.expression}
                onChange={setExpression}
                issues={issues}
                onCaretChange={setCaret}
                aria-describedby={`${editorId}-status`}
              />
              <div id={`${editorId}-status`} className="flex min-h-[20px] flex-wrap items-center gap-x-3 gap-y-1 text-xs" aria-live="polite">
                {validating ? (
                  <span className="inline-flex items-center gap-1.5 text-muted">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Validating…
                  </span>
                ) : errors.length > 0 ? (
                  <span className="inline-flex items-center gap-1.5 text-danger">
                    <AlertCircle className="h-3.5 w-3.5" /> {errors.length} error{errors.length === 1 ? '' : 's'}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-success">
                    <CheckCircle2 className="h-3.5 w-3.5" /> Valid
                  </span>
                )}
                {warnings.length > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-warning">
                    <AlertTriangle className="h-3.5 w-3.5" /> {warnings.length} warning{warnings.length === 1 ? '' : 's'}
                  </span>
                )}
                {caretIssue && <span className="truncate text-muted">At cursor: {caretIssue.message}</span>}
                {validate.isError && <span className="text-danger">Validation unavailable: {String(validate.error.message)}</span>}
              </div>
              {issues.length > 0 && (
                <ul className="flex flex-col gap-1" aria-label="Problems">
                  {issues.map((iss, i) => {
                    const { line, col } = lineCol(draft.expression, iss.start);
                    return (
                      <li key={`${iss.start}-${iss.end}-${i}`}>
                        <button
                          type="button"
                          onClick={() => editor.current?.select(iss.start, Math.max(iss.end, iss.start + 1))}
                          className={cn(
                            'flex w-full items-start gap-2 rounded-md px-2 py-1 text-left text-xs hover:bg-ink-850',
                            iss.severity === 'error' ? 'text-danger' : 'text-warning',
                          )}
                        >
                          {iss.severity === 'error' ? (
                            <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                          ) : (
                            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                          )}
                          <span className="flex-1">{iss.message}</span>
                          <span className="font-mono text-2xs text-muted">
                            {line}:{col}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
              {warnings.length > 0 && errors.length === 0 && (
                <p className="text-2xs text-muted">Warnings don’t block saving. Empty blocks simply render nothing for files that lack that value.</p>
              )}
              <div>
                <h3 className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">Methods</h3>
                {catalog.data ? (
                  <MethodChips
                    filters={catalog.data.filters}
                    onInsert={(f) => {
                      const takesArgs = /\(\s*[^)\s]/.test(f.signature);
                      editor.current?.insert(`.${f.name}()`, { caretOffset: takesArgs ? f.name.length + 2 : f.name.length + 3 });
                    }}
                  />
                ) : (
                  <Skeleton className="h-6 w-full" />
                )}
              </div>
            </div>
          </Card>

          <Card title="Live preview" description="Built-in sample files for this kind, rendered as you type." flush>
            {!validate.data && validate.isFetching ? (
              <SkeletonRows rows={4} className="p-4" />
            ) : !validate.data ? (
              <EmptyState compact title="Type a format to see samples" />
            ) : validate.data.samples.length === 0 ? (
              <p className="px-4 py-4 text-xs text-muted">No samples returned.</p>
            ) : (
              <div className={cn('divide-y divide-ink-800 transition-opacity', validating && 'opacity-60')}>
                {validate.data.samples.map((s) => (
                  <div key={s.source} className="grid gap-1 px-4 py-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)] md:gap-4">
                    <span className="truncate font-mono text-2xs text-muted" title={s.source}>
                      {s.source}
                    </span>
                    {s.error ? (
                      <span className="text-xs text-danger">{s.error}</span>
                    ) : (
                      <PathText path={s.output} compareTo={s.source} wrap placeholder="(renders empty)" />
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card
            title="Try with your files"
            description="Paste filenames (one per line). Schema parses and matches them, then renders this format. Nothing is moved."
            icon={<FlaskConical />}
          >
            <div className="flex flex-col gap-2">
              <Textarea
                mono
                rows={3}
                value={tryNames}
                onChange={(e) => setTryNames(e.target.value)}
                placeholder={'Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv\n[SubsPlease] Kaze no Tsubasa - 15 (1080p).mkv'}
                aria-label="Filenames to preview"
              />
              <div className="flex justify-end">
                <Button size="sm" onClick={runPreview} loading={preview.isPending} disabled={!tryNames.trim() || errors.length > 0}>
                  Preview
                </Button>
              </div>
              {preview.data && (
                <div className="divide-y divide-ink-800 rounded-lg border border-ink-800">
                  {preview.data.map((p) => (
                    <div key={p.name} className="flex flex-col gap-1 px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate font-mono text-2xs text-muted">{p.name}</span>
                        {p.best && (
                          <span className="hidden truncate text-2xs text-muted sm:inline">
                            {p.best.title}
                            {p.best.year ? ` (${p.best.year})` : ''}
                          </span>
                        )}
                        <ConfidenceMeter value={p.confidence} />
                      </div>
                      {p.error ? (
                        <span className="text-xs text-danger">{p.error}</span>
                      ) : (
                        <PathText path={p.rendered} compareTo={p.name} wrap placeholder="No match, nothing rendered" />
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Card>

          <Card title="Presets" description="Plex, Jellyfin, Emby and Kodi layouts. Click to load into the editor.">
            {catalog.data ? (
              <PresetGallery
                presets={catalog.data.presets}
                kind={draft.kind}
                current={draft.expression}
                onPick={(p) => {
                  setDraft((d) => ({ ...d, expression: p.expression, id: null, builtin: false, is_default: false, name: p.name }));
                  setLoadedFrom(null);
                }}
              />
            ) : catalog.isError ? (
              <ErrorState compact error={catalog.error} onRetry={() => void catalog.refetch()} />
            ) : (
              <SkeletonRows rows={3} />
            )}
          </Card>

          <Card title="Import a FileBot format" icon={<FileInput />}>
            <div className="flex flex-col gap-2">
              <Textarea
                mono
                rows={2}
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
                placeholder="{plex.tail} or {n} - {s00e00} - {t}"
                aria-label="FileBot format to import"
              />
              <div className="flex items-center justify-between gap-2">
                <p className="text-2xs text-muted">Pasted verbatim. Unsupported bindings and methods are flagged as warnings.</p>
                <Button
                  size="sm"
                  disabled={!importText.trim()}
                  onClick={() => {
                    setDraft((d) => ({ ...d, expression: importText.trim(), id: null, builtin: false, is_default: false, name: 'Imported from FileBot' }));
                    setLoadedFrom(null);
                    setImportedNote(true);
                    setImportText('');
                  }}
                >
                  Load into editor
                </Button>
              </div>
              {importedNote && (
                <p className="rounded-md bg-ink-850 px-3 py-2 text-xs text-muted">
                  Imported. {issues.length === 0 && !validating ? 'Everything is supported.' : 'Check the problems listed under the editor.'}
                </p>
              )}
            </div>
          </Card>
        </div>

        {/* -------------------------------------------- bindings */}
        <Card title="Bindings" description="Click to insert at the cursor." className="lg:col-span-2 xl:col-span-1 xl:self-start">
          {catalog.data ? (
            <div className="xl:max-h-[calc(100vh-220px)] xl:overflow-y-auto">
              <BindingPalette
                bindings={catalog.data.bindings}
                kind={draft.kind}
                used={used}
                onInsert={(name) => editor.current?.insert(name, { binding: true })}
              />
            </div>
          ) : catalog.isError ? (
            <ErrorState compact error={catalog.error} onRetry={() => void catalog.refetch()} />
          ) : (
            <SkeletonRows rows={6} />
          )}
        </Card>
      </div>

      <Dialog
        open={saveOpen}
        onClose={() => setSaveOpen(false)}
        title="Save format"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={doSaveAs} loading={create.isPending} disabled={!saveName.trim()}>
              Save
            </Button>
          </>
        }
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            doSaveAs();
          }}
        >
          <Field label="Name">
            <Input value={saveName} onChange={(e) => setSaveName(e.target.value)} maxLength={120} data-autofocus />
          </Field>
          <Field label="Kind">
            <Select<EditKind>
              value={draft.kind}
              onChange={(k) => setDraft((d) => ({ ...d, kind: k }))}
              options={KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))}
            />
          </Field>
          <Field inline label={`Default for ${KIND_LABEL[draft.kind].toLowerCase()}`} hint="Used when a job doesn’t pick a format.">
            <Switch checked={saveDefault} onChange={setSaveDefault} />
          </Field>
          {warnings.length > 0 && (
            <p className="text-xs text-warning">
              {warnings.length} warning{warnings.length === 1 ? '' : 's'}: you can still save.
            </p>
          )}
        </form>
      </Dialog>

      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() =>
          draft.id !== null &&
          del.mutate(draft.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              newFormat();
            },
          })
        }
        loading={del.isPending}
        tone="danger"
        title={`Delete “${draft.name}”?`}
        description="Jobs and watch folders that use it fall back to the default format for their kind."
        confirmLabel="Delete format"
      />
    </Page>
    </>
  );
}
