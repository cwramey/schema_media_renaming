import { ButtonLink, EmptyState } from '@/components/ui';
import { useDocumentTitle } from '@/lib/hooks';

export function NotFoundPage() {
  useDocumentTitle('Not found');
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <EmptyState
        title="Nothing here"
        description="This page doesn’t exist. It may have been a job that was deleted."
        action={
          <ButtonLink to="/" variant="primary">
            Back to Home
          </ButtonLink>
        }
      />
    </div>
  );
}
