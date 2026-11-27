import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import type { BindingSpec, FilterSpec, FormatPreset, MediaKind } from '@/api/types';
import { cn } from '@/lib/cn';
import { Input, Tooltip } from '@/components/ui';

/** Group order follows SPEC §8.5. */
const GROUP_ORDER = ['Naming', 'Episode', 'Movie', 'Info', 'IDs', 'Quality', 'File', 'Presets'];

function TipBody({ title, description, example }: { title: string; description: string; example?: string }) {
  return (
    <span className="flex flex-col gap-1">
      <span className="font-mono font-semibold text-accent">{title}</span>
      <span>{description}</span>
      {example && (
        <span className="rounded bg-ink-950 px-1.5 py-0.5 font-mono text-2xs text-ice-100">{example}</span>
      )}
    </span>
  );
}

/** Binding palette grouped as in SPEC §8.5: click inserts, hover/focus shows description + example. */
export function BindingPalette({
  bindings,
  kind,
  used,
  onInsert,
}: {
  bindings: BindingSpec[];
  kind: MediaKind;
  used: Set<string>;
  onInsert: (name: string) => void;
}) {
  const [q, setQ] = useState('');
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const visible = bindings.filter(
      (b) =>
        (b.kinds.length === 0 || b.kinds.includes(kind)) &&
        (!needle || b.name.toLowerCase().includes(needle) || b.description.toLowerCase().includes(needle)),
    );
    const map = new Map<string, BindingSpec[]>();
    for (const b of visible) {
      const list = map.get(b.group) ?? [];
      list.push(b);
      map.set(b.group, list);
    }
    const rank = (g: string) => {
      const i = GROUP_ORDER.indexOf(g);
      return i >= 0 ? i : GROUP_ORDER.length;
    };
    return [...map.entries()].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
  }, [bindings, kind, q]);

  return (
    <div className="flex flex-col gap-3">
      <Input
        size="sm"
        leftIcon={<Search />}
        placeholder="Find a binding"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        aria-label="Find a binding"
      />
      {groups.length === 0 && <p className="text-xs text-muted">No bindings match.</p>}
      {groups.map(([group, list]) => (
        <div key={group}>
          <h4 className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">{group}</h4>
          <div className="flex flex-wrap gap-1">
            {list.map((b) => (
              <Tooltip
                key={b.name}
                content={<TipBody title={`{${b.name}}`} description={b.description} example={b.example} />}
              >
                <button
                  type="button"
                  onClick={() => onInsert(b.name)}
                  className={cn(
                    'h-6 rounded-md border px-1.5 font-mono text-2xs transition-colors',
                    used.has(b.name.split('.')[0])
                      ? 'border-brand-sky/50 bg-brand-sky/10 text-accent'
                      : 'border-ink-800 bg-ink-950 text-ice-100 hover:border-ink-700 hover:bg-ink-850',
                  )}
                >
                  {b.name}
                </button>
              </Tooltip>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Method chips (SPEC §8.4): insert `.name()` with the caret between the parentheses. */
export function MethodChips({ filters, onInsert }: { filters: FilterSpec[]; onInsert: (f: FilterSpec) => void }) {
  return (
    <div className="flex flex-wrap gap-1">
      {filters.map((f) => (
        <Tooltip key={f.name} content={<TipBody title={f.signature} description={f.description} example={f.example} />}>
          <button
            type="button"
            onClick={() => onInsert(f)}
            className="h-6 rounded-md border border-ink-800 bg-ink-950 px-1.5 font-mono text-2xs text-[#7A33B3] transition-colors hover:border-ink-700 hover:bg-ink-850 dark:text-[#C49BEA]"
          >
            .{f.name}()
          </button>
        </Tooltip>
      ))}
    </div>
  );
}

export function PresetGallery({
  presets,
  kind,
  current,
  onPick,
}: {
  presets: FormatPreset[];
  kind: MediaKind;
  current: string;
  onPick: (p: FormatPreset) => void;
}) {
  const list = presets.filter((p) => p.kind === kind);
  if (list.length === 0) return <p className="text-xs text-muted">No presets for this kind.</p>;
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {list.map((p) => {
        const active = p.expression === current;
        return (
          <li key={p.id}>
            <button
              type="button"
              onClick={() => onPick(p)}
              aria-pressed={active}
              className={cn(
                'flex h-full w-full flex-col gap-1 rounded-lg border p-2.5 text-left transition-colors',
                active ? 'border-brand-sky/60 bg-brand-sky/[0.07]' : 'border-ink-800 hover:border-ink-700 hover:bg-ink-850/60',
              )}
            >
              <span className="text-[13px] font-medium">{p.name}</span>
              <span className="text-2xs text-muted">{p.description}</span>
              <code className="mt-1 break-all font-mono text-2xs text-ice-100/80">{p.expression}</code>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
