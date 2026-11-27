import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import { ApiError, errorMessage } from './client';
import { toast } from '@/lib/toast';

declare module '@tanstack/react-query' {
  interface Register {
    defaultError: Error;
    mutationMeta: {
      /** Toast title on failure; omit for a generic one. */
      errorTitle?: string;
      /** Skip the global error toast (the caller shows the error inline). */
      silent?: boolean;
      /** Toast title on success. */
      successTitle?: string;
    };
    queryMeta: {
      silent?: boolean;
    };
  }
}

export function createQueryClient(): QueryClient {
  const qc: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        // First-load failures render inline error states; only toast when stale data is on screen.
        if (query.meta?.silent || query.state.data === undefined) return;
        if (error instanceof ApiError && error.isUnauthorized) return;
        toast.error('Could not refresh data', {
          description: errorMessage(error),
          action: { label: 'Retry', onClick: () => void qc.refetchQueries({ queryKey: query.queryKey }) },
        });
      },
    }),
    mutationCache: new MutationCache({
      onSuccess: (_data, _vars, _ctx, mutation) => {
        if (mutation.meta?.successTitle) toast.success(mutation.meta.successTitle);
      },
      onError: (error, variables, _ctx, mutation) => {
        if (mutation.meta?.silent) return;
        if (error instanceof ApiError && error.isUnauthorized) return;
        const retryable = !(error instanceof ApiError) || error.isRetryable;
        toast.error(mutation.meta?.errorTitle ?? 'Something went wrong', {
          description: errorMessage(error),
          action: retryable
            ? { label: 'Retry', onClick: () => void mutation.execute(variables).catch(() => undefined) }
            : undefined,
        });
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => {
          if (error instanceof ApiError && !error.isRetryable) return false;
          return count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
  return qc;
}
