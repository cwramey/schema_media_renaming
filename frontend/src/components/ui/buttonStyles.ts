import { cn } from '@/lib/cn';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'subtle' | 'outline';
export type ButtonSize = 'xs' | 'sm' | 'md' | 'lg';

export const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'btn-gradient text-[#0F1216] font-semibold shadow-glow hover:brightness-105 disabled:shadow-none',
  secondary: 'bg-ink-850 text-ice-100 border border-ink-800 hover:bg-ink-800 hover:border-ink-700',
  outline: 'bg-transparent text-ice-100 border border-ink-700 hover:bg-ink-850',
  ghost: 'bg-transparent text-ice-100 hover:bg-ink-850',
  subtle: 'bg-transparent text-muted hover:text-ice-100 hover:bg-ink-850',
  danger: 'bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20',
};

export const SIZES: Record<ButtonSize, string> = {
  xs: 'h-6 px-2 text-xs gap-1 rounded-md',
  sm: 'h-7 px-2.5 text-xs gap-1.5 rounded-md',
  md: 'h-8 px-3 text-[13px] gap-2 rounded-lg',
  lg: 'h-10 px-4 text-sm gap-2 rounded-lg',
};

export const ICON_SIZES: Record<ButtonSize, string> = {
  xs: 'h-6 w-6 rounded-md',
  sm: 'h-7 w-7 rounded-md',
  md: 'h-8 w-8 rounded-lg',
  lg: 'h-10 w-10 rounded-lg',
};

export function buttonClasses(variant: ButtonVariant = 'secondary', size: ButtonSize = 'md', block?: boolean) {
  return cn(
    'inline-flex select-none items-center justify-center whitespace-nowrap font-medium transition-colors',
    VARIANTS[variant],
    SIZES[size],
    block && 'w-full',
  );
}

