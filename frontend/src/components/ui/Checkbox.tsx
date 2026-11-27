import { forwardRef, useEffect, useImperativeHandle, useRef, type InputHTMLAttributes, type ReactNode } from 'react';
import { Check, Minus } from 'lucide-react';
import { cn } from '@/lib/cn';

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange'> {
  checked: boolean;
  indeterminate?: boolean;
  onChange: (checked: boolean, event: React.ChangeEvent<HTMLInputElement>) => void;
  label?: ReactNode;
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { checked, indeterminate = false, onChange, label, className, disabled, ...rest },
  ref,
) {
  const inner = useRef<HTMLInputElement>(null);
  useImperativeHandle(ref, () => inner.current as HTMLInputElement);
  useEffect(() => {
    if (inner.current) inner.current.indeterminate = indeterminate;
  }, [indeterminate]);

  const box = (
    <span className={cn('relative inline-flex h-4 w-4 shrink-0 items-center justify-center', !label && className)}>
      <input
        ref={inner}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked, e)}
        className={cn(
          'peer h-4 w-4 cursor-pointer appearance-none rounded border border-ink-700 bg-ink-950 transition-colors',
          'checked:border-brand-sky checked:bg-brand-sky indeterminate:border-brand-sky indeterminate:bg-brand-sky',
          'hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-50',
        )}
        {...rest}
      />
      {indeterminate ? (
        <Minus className="pointer-events-none absolute h-3 w-3 text-[#0F1216]" strokeWidth={3} aria-hidden />
      ) : (
        checked && <Check className="pointer-events-none absolute h-3 w-3 text-[#0F1216]" strokeWidth={3} aria-hidden />
      )}
    </span>
  );
  if (!label) return box;
  return (
    <label className={cn('inline-flex cursor-pointer items-center gap-2 text-[13px]', disabled && 'opacity-60', className)}>
      {box}
      <span>{label}</span>
    </label>
  );
});

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  disabled?: boolean;
  id?: string;
  size?: 'sm' | 'md';
  className?: string;
  'aria-label'?: string;
  'aria-describedby'?: string;
}

export function Switch({ checked, onChange, label, disabled, id, size = 'md', className, ...aria }: SwitchProps) {
  const track = size === 'sm' ? 'h-4 w-7' : 'h-5 w-9';
  const knob = size === 'sm' ? 'h-3 w-3' : 'h-4 w-4';
  const shift = size === 'sm' ? 'translate-x-3' : 'translate-x-4';
  const sw = (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-50',
        track,
        checked ? 'bg-brand-sky' : 'bg-ink-700',
        !label && className,
      )}
      {...aria}
    >
      <span
        className={cn(
          'inline-block translate-x-0.5 rounded-full bg-white shadow transition-transform',
          knob,
          checked && shift,
        )}
      />
    </button>
  );
  if (!label) return sw;
  return (
    <label className={cn('inline-flex cursor-pointer items-center gap-2 text-[13px]', className)}>
      {sw}
      <span>{label}</span>
    </label>
  );
}
