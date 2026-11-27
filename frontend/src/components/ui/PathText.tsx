import { useMemo } from 'react';
import { cn } from '@/lib/cn';
import { diffPath, relativeTo, splitPath, type DiffKind } from '@/lib/pathDiff';

export interface PathTextProps {
  path: string | null | undefined;
  /**
   * Original filename to diff against: words of the new filename that are not in it
   * get highlighted. Without it the whole filename is highlighted.
   */
  compareTo?: string;
  /** Strip this root prefix for display (full path stays in the tooltip). */
  root?: string | null;
  className?: string;
  /** Allow wrapping instead of single-line truncation. */
  wrap?: boolean;
  placeholder?: string;
}

const KIND_CLASS: Record<DiffKind, string> = {
  root: 'text-muted/70',
  folder: 'text-muted',
  sep: 'text-muted/60',
  same: 'text-ice-100',
  changed: 'text-accent font-semibold',
  ext: 'text-muted',
};

/**
 * Monospace path: folders dimmed, filename emphasized, and (with `compareTo`) the changed
 * words of the filename highlighted. Truncates folders first so the filename stays visible
 * (the folder part has a much larger flex-shrink; factors below 1 would leave overflow).
 */
export function PathText({ path, compareTo, root, className, wrap, placeholder = '—' }: PathTextProps) {
  const shown = path ? relativeTo(path, root) : '';
  const { head, tail } = useMemo(() => {
    if (!shown) return { head: [], tail: [] };
    const segs = compareTo
      ? diffPath(compareTo, shown)
      : (() => {
          const p = splitPath(shown);
          const out: { text: string; kind: DiffKind }[] = [];
          if (p.root) out.push({ text: p.root, kind: 'root' });
          p.folders.forEach((f) => out.push({ text: f, kind: 'folder' }, { text: p.sep, kind: 'sep' }));
          out.push({ text: p.stem, kind: 'same' });
          if (p.ext) out.push({ text: `.${p.ext}`, kind: 'ext' });
          return out;
        })();
    // Split into the directory part (head) and the filename part (tail).
    let lastDirIdx = -1;
    segs.forEach((s, i) => {
      if (s.kind === 'root' || s.kind === 'folder') lastDirIdx = i + (segs[i + 1]?.kind === 'sep' ? 1 : 0);
    });
    return { head: segs.slice(0, lastDirIdx + 1), tail: segs.slice(lastDirIdx + 1) };
  }, [shown, compareTo]);

  if (!path) return <span className={cn('font-mono text-xs text-muted', className)}>{placeholder}</span>;

  if (wrap) {
    return (
      <span className={cn('break-all font-mono text-xs leading-5', className)} title={path}>
        {[...head, ...tail].map((s, i) => (
          <span key={i} className={KIND_CLASS[s.kind]}>
            {s.text}
          </span>
        ))}
      </span>
    );
  }

  return (
    <span className={cn('flex min-w-0 font-mono text-xs leading-5', className)} title={path}>
      {head.length > 0 && (
        <span className="min-w-[2ch] shrink-[1000] truncate">
          {head.map((s, i) => (
            <span key={i} className={KIND_CLASS[s.kind]}>
              {s.text}
            </span>
          ))}
        </span>
      )}
      <span className="min-w-0 shrink truncate">
        {tail.map((s, i) => (
          <span key={i} className={KIND_CLASS[s.kind]}>
            {s.text}
          </span>
        ))}
      </span>
    </span>
  );
}

/** Plain mono filename with the extension dimmed. */
export function FileName({ name, className }: { name: string; className?: string }) {
  const p = splitPath(name);
  return (
    <span className={cn('block truncate font-mono text-xs', className)} title={name}>
      <span className="text-ice-100">{p.stem}</span>
      {p.ext && <span className="text-muted">.{p.ext}</span>}
    </span>
  );
}
