import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { useDocumentTitle } from '@/lib/hooks';

export function Page({
  title,
  description,
  actions,
  children,
  className,
  width = 'default',
  docTitle,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  width?: 'default' | 'narrow' | 'wide';
  docTitle?: string;
}) {
  useDocumentTitle(docTitle ?? (typeof title === 'string' ? title : undefined));
  return (
    <div
      className={cn(
        'mx-auto w-full px-4 py-5 md:px-6 md:py-6',
        width === 'narrow' ? 'max-w-3xl' : width === 'wide' ? 'max-w-[1600px]' : 'max-w-7xl',
        className,
      )}
    >
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight md:text-xl">{title}</h1>
          {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
