import {
  cloneElement,
  forwardRef,
  isValidElement,
  useId,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/cn';

export const controlBase =
  'w-full rounded-lg border border-ink-800 bg-ink-950 text-ice-100 placeholder:text-muted/70 ' +
  'transition-colors hover:border-ink-700 focus:border-ring focus:outline-none focus-visible:outline-none ' +
  'focus:ring-2 focus:ring-ring/30 disabled:cursor-not-allowed disabled:opacity-60 aria-[invalid=true]:border-danger';

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  leftIcon?: ReactNode;
  rightSlot?: ReactNode;
  mono?: boolean;
  size?: 'sm' | 'md';
  invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { leftIcon, rightSlot, mono, size = 'md', invalid, className, ...rest },
  ref,
) {
  return (
    <div className={cn('relative flex items-center', className)}>
      {leftIcon && (
        <span className="pointer-events-none absolute left-2.5 flex text-muted [&>svg]:h-3.5 [&>svg]:w-3.5">
          {leftIcon}
        </span>
      )}
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          controlBase,
          size === 'sm' ? 'h-7 text-xs' : 'h-8 text-[13px]',
          leftIcon ? 'pl-8' : 'pl-2.5',
          rightSlot ? 'pr-9' : 'pr-2.5',
          mono && 'font-mono text-xs',
        )}
        {...rest}
      />
      {rightSlot && <span className="absolute right-1 flex items-center">{rightSlot}</span>}
    </div>
  );
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  mono?: boolean;
  invalid?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { mono, invalid, className, ...rest },
  ref,
) {
  return (
    <textarea
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(controlBase, 'min-h-[80px] px-2.5 py-2 text-[13px] leading-5', mono && 'font-mono text-xs', className)}
      {...rest}
    />
  );
});

export interface SelectOption<V extends string = string> {
  value: V;
  label: string;
  disabled?: boolean;
}

export interface SelectProps<V extends string = string>
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange' | 'value' | 'size'> {
  value: V;
  onChange: (value: V) => void;
  options: readonly SelectOption<V>[];
  size?: 'sm' | 'md';
}

/** Native <select> (best keyboard/AT support), styled to match. */
export function Select<V extends string = string>({
  value,
  onChange,
  options,
  size = 'md',
  className,
  ...rest
}: SelectProps<V>) {
  return (
    <div className={cn('relative', className)}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as V)}
        className={cn(
          controlBase,
          'appearance-none pl-2.5 pr-8',
          size === 'sm' ? 'h-7 text-xs' : 'h-8 text-[13px]',
        )}
        {...rest}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
    </div>
  );
}

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactElement;
  className?: string;
  /** Put label and control on one row (switches, checkboxes). */
  inline?: boolean;
  optional?: boolean;
}

/** Label + control + hint/error, wired with ids for accessibility. */
export function Field({ label, hint, error, children, className, inline, optional }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const describedBy = [hint ? hintId : null, error ? `${id}-err` : null].filter(Boolean).join(' ') || undefined;
  const control = isValidElement<{ id?: string; 'aria-describedby'?: string }>(children)
    ? cloneElement(children, { id: children.props.id ?? id, 'aria-describedby': describedBy })
    : children;
  return (
    <div className={cn(inline ? 'flex items-start justify-between gap-4' : 'flex flex-col gap-1.5', className)}>
      <div className={cn(inline && 'min-w-0 flex-1')}>
        <label htmlFor={id} className="text-xs font-medium text-ice-100">
          {label}
          {optional && <span className="ml-1 font-normal text-muted">(optional)</span>}
        </label>
        {inline && hint && (
          <p id={hintId} className="mt-0.5 text-xs text-muted">
            {hint}
          </p>
        )}
      </div>
      {control}
      {!inline && hint && !error && (
        <p id={hintId} className="text-xs text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-err`} className="text-xs text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
