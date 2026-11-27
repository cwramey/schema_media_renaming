import { useState } from 'react';
import { Undo2 } from 'lucide-react';
import { useUndoOperation } from '@/api/hooks';
import { cn } from '@/lib/cn';
import { Button, ConfirmDialog, PathText, type ButtonVariant } from '@/components/ui';

/** "Undo" with a confirmation, for one journaled operation (Home library cards, History rows). */
export function UndoOperationButton({
  operationId,
  title,
  destination,
  source,
  variant = 'outline',
  compact,
  className,
}: {
  operationId: number;
  title: string;
  destination: string | null;
  source?: string | null;
  variant?: ButtonVariant;
  /** Icon plus label, with the label hidden on phones (Home's library list). */
  compact?: boolean;
  className?: string;
}) {
  const undo = useUndoOperation();
  const [confirm, setConfirm] = useState(false);
  return (
    <>
      <Button
        variant={variant}
        size="lg"
        className={cn('px-3.5 text-sm', className)}
        aria-label={`Undo ${title}`}
        leftIcon={compact ? <Undo2 className="h-4 w-4" aria-hidden /> : undefined}
        onClick={() => setConfirm(true)}
      >
        {compact ? <span className="max-sm:sr-only">Undo</span> : 'Undo'}
      </Button>
      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => undo.mutate(operationId, { onSettled: () => setConfirm(false) })}
        loading={undo.isPending}
        title={`Undo ${title}?`}
        description="Schema puts the file back exactly where it was and restores anything it replaced. This is refused if the file changed since."
        confirmLabel="Undo rename"
      >
        {(destination || source) && (
          <div className="flex flex-col gap-1.5 rounded-lg border border-ink-800 bg-ink-950 p-3">
            {destination && <PathText path={destination} wrap />}
            {source && (
              <>
                <span className="flex items-center gap-1 text-2xs text-muted">
                  <Undo2 className="h-3 w-3" aria-hidden /> back to
                </span>
                <PathText path={source} wrap />
              </>
            )}
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}
