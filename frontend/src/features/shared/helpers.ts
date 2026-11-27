import { Radar, Upload, Webhook } from 'lucide-react';
import type { JobOut, ProviderStatus } from '@/api/types';

export const SOURCE_ICON = { manual: Upload, watch: Radar, webhook: Webhook } as const;

export function jobTitle(job: Pick<JobOut, 'id' | 'label'>) {
  return job.label || `Job #${job.id}`;
}

/** "18 matched · 3 review · 1 skipped" */
export function countsSummary(job: JobOut): string {
  const order: [keyof JobOut['counts'], string][] = [
    ['review', 'review'],
    ['matched', 'matched'],
    ['approved', 'approved'],
    ['done', 'done'],
    ['skipped', 'skipped'],
    ['error', 'errors'],
    ['pending', 'pending'],
  ];
  const parts = order.filter(([k]) => (job.counts[k] ?? 0) > 0).map(([k, label]) => `${job.counts[k]} ${label}`);
  return parts.length ? parts.join(' · ') : `${job.total} files`;
}

export function providerTone(p: ProviderStatus) {
  if (!p.enabled) return 'neutral' as const;
  if (!p.configured) return p.requires_key ? ('warning' as const) : ('neutral' as const);
  if (p.healthy === false) return 'danger' as const;
  if (p.healthy === true) return 'success' as const;
  return 'info' as const;
}

export function providerStateLabel(p: ProviderStatus) {
  if (!p.enabled) return 'Disabled';
  if (!p.configured) return p.requires_key ? 'Needs key' : 'Not configured';
  if (p.healthy === false) return 'Failing';
  if (p.healthy === true) return 'Healthy';
  return 'Not checked';
}

