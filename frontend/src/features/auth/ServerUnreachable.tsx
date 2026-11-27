import { RotateCw, ServerCrash } from 'lucide-react';
import { errorMessage } from '@/api/client';
import { Button } from '@/components/ui';
import { BrandLayout } from './AuthPages';

export function ServerUnreachable({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <BrandLayout>
      <div className="flex flex-col gap-4">
        <div className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-danger/10 text-danger">
          <ServerCrash className="h-5 w-5" />
        </div>
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Can’t reach the Schema server</h1>
          <p className="mt-1 text-[13px] text-muted">{errorMessage(error)}</p>
          <p className="mt-3 text-xs text-muted">
            Check that the container is running and that this page is served from it (port 8420 by default).
          </p>
        </div>
        <Button variant="primary" leftIcon={<RotateCw className="h-4 w-4" />} onClick={onRetry}>
          Try again
        </Button>
      </div>
    </BrandLayout>
  );
}
