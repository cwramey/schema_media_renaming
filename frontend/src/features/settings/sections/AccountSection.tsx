import { useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Download, KeyRound, LogOut, RefreshCw, RotateCcw, Upload } from 'lucide-react';
import { errorMessage, isApiError } from '@/api/client';
import { useAuthStatus, useChangePassword, useDemoReset, useHealth, useLogout, useLogoutAll } from '@/api/hooks';
import { Button, Checkbox, ConfirmDialog, Dialog, Field } from '@/components/ui';
import { PasswordInput } from '@/features/auth/AuthPages';
import { useTheme, type ThemePref } from '@/lib/theme';
import { toast } from '@/lib/toast';
import { downloadJson, fetchBackup, invalidateAllSettings, restoreBackup } from '../api';
import { ConnStatus, Row, Segmented, SettingsSection } from '../controls';
import { backupFileName, looksLikeBackup } from '../mapping';

const THEMES: { value: ThemePref; label: string }[] = [
  { value: 'system', label: 'Match system' },
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
];

export function AccountSection() {
  const { data: auth } = useAuthStatus();
  const { pref, setPref } = useTheme();
  const authOff = auth ? !auth.auth_enabled : false;

  return (
    <SettingsSection id="account" num={8} title="Account & app" description="Your sign-in, how Schema looks, and backups.">
      {authOff ? (
        <Row id="set-account-off" group label="Password" help="Sign-in is turned off on this server (SCHEMA_AUTH=disabled), so there’s no password to change or session to end.">
          <p className="text-[13px] text-muted">Nothing to set here.</p>
        </Row>
      ) : (
        <>
          <PasswordRow username={auth?.username ?? null} />
          <SessionsRow />
        </>
      )}
      <Row id="set-theme" group label="Theme" help="Saved in this browser only.">
        <Segmented id="set-theme" labelledBy="set-theme-label" describedBy="set-theme-help" options={THEMES} value={pref} onChange={setPref} />
      </Row>
      <BackupRow />
      <AboutRow />
      {auth?.demo && <DemoRow />}
    </SettingsSection>
  );
}

/* ------------------------------------------------------------------ password */

function PasswordRow({ username }: { username: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <Row id="set-password" group label="Password" help="Change the password you use to sign in to Schema.">
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" className="h-10" leftIcon={<KeyRound className="h-3.5 w-3.5" />} onClick={() => setOpen(true)}>
          Change password
        </Button>
      </div>
      {open && <ChangePasswordDialog username={username} onClose={() => setOpen(false)} />}
    </Row>
  );
}

function ChangePasswordDialog({ username, onClose }: { username: string | null; onClose: () => void }) {
  const change = useChangePassword();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [touched, setTouched] = useState(false);
  const tooShort = next.length < 8;
  const mismatch = confirm.length > 0 && confirm !== next;

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    setTouched(true);
    if (!current || tooShort || confirm !== next) return;
    change.mutate({ current_password: current, new_password: next }, { onSuccess: onClose });
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Change password"
      description={`${username ? `Signed in as ${username}. ` : ''}Changing it signs out every other browser and phone.`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={change.isPending} onClick={() => submit()}>
            Change password
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <input type="text" name="username" autoComplete="username" value={username ?? ''} readOnly hidden />
        <Field label="Current password" error={touched && !current ? 'Required' : undefined}>
          <PasswordInput value={current} onChange={setCurrent} autoComplete="current-password" invalid={touched && !current} />
        </Field>
        <Field label="New password" hint="At least 8 characters." error={touched && tooShort ? 'At least 8 characters' : undefined}>
          <PasswordInput value={next} onChange={setNext} autoComplete="new-password" invalid={touched && tooShort} />
        </Field>
        <Field label="Type the new password again" error={mismatch ? 'The passwords don’t match' : undefined}>
          <PasswordInput value={confirm} onChange={setConfirm} autoComplete="new-password" invalid={mismatch} />
        </Field>
        {change.isError && (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            {isApiError(change.error) && change.error.isUnauthorized ? 'The current password is wrong.' : errorMessage(change.error)}
          </p>
        )}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ sessions */

function SessionsRow() {
  const logout = useLogout();
  const logoutAll = useLogoutAll();
  const navigate = useNavigate();
  const [confirm, setConfirm] = useState(false);
  return (
    <Row
      id="set-sessions"
      group
      label="Signed-in devices"
      help="Sign out here, or sign out every browser and phone at once if you think someone else has access."
    >
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          className="h-10"
          leftIcon={<LogOut className="h-3.5 w-3.5" />}
          loading={logout.isPending}
          onClick={() => logout.mutate(undefined, { onSuccess: () => navigate('/login', { replace: true }) })}
        >
          Sign out
        </Button>
        <Button variant="danger" className="h-10" onClick={() => setConfirm(true)}>
          Sign out everywhere
        </Button>
      </div>
      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          logoutAll.mutate(undefined, {
            onSuccess: () => navigate('/login', { replace: true }),
            onSettled: () => setConfirm(false),
          })
        }
        loading={logoutAll.isPending}
        tone="danger"
        title="Sign out everywhere?"
        description="Every browser and phone signed in to Schema, including this one, is signed out and has to sign in again."
        confirmLabel="Sign out everywhere"
      />
    </Row>
  );
}

/* ------------------------------------------------------------------ backup & restore */

function BackupRow() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [secrets, setSecrets] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [pending, setPending] = useState<{ name: string; data: unknown; created?: string } | null>(null);
  const [restoring, setRestoring] = useState(false);

  const download = async () => {
    setDownloading(true);
    try {
      downloadJson(await fetchBackup(secrets), backupFileName());
    } catch (e) {
      toast.error('Couldn’t make the backup', { description: errorMessage(e) });
    } finally {
      setDownloading(false);
    }
  };

  const pick = async (file: File | undefined) => {
    if (!file) return;
    try {
      const data: unknown = JSON.parse(await file.text());
      if (!looksLikeBackup(data)) throw new Error('This file isn’t a Schema settings backup.');
      setPending({ name: file.name, data, created: typeof data.created_at === 'string' ? data.created_at : undefined });
    } catch (e) {
      toast.error('Couldn’t read that file', { description: e instanceof SyntaxError ? 'It isn’t valid JSON.' : errorMessage(e) });
    } finally {
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const restore = async () => {
    if (!pending) return;
    setRestoring(true);
    try {
      const r = await restoreBackup(pending.data);
      const parts = [
        r.app && 'app settings',
        r.providers && 'title sources',
        r.watch_folders > 0 && `${r.watch_folders} watch folder${r.watch_folders === 1 ? '' : 's'}`,
        r.formats > 0 && `${r.formats} format${r.formats === 1 ? '' : 's'}`,
      ].filter(Boolean);
      toast.success('Settings restored', { description: parts.length ? `Restored ${parts.join(', ')}.` : 'Nothing needed changing.' });
      setPending(null);
    } catch (e) {
      toast.error('Couldn’t restore the backup', { description: errorMessage(e) });
    } finally {
      setRestoring(false);
      invalidateAllSettings(qc);
    }
  };

  return (
    <Row
      id="set-backup"
      group
      label="Back up settings"
      help="Download all settings as one file, or restore them after reinstalling Schema."
    >
      <div className="flex flex-col gap-2.5">
        <Checkbox
          checked={secrets}
          onChange={setSecrets}
          label="Include API keys and tokens"
          aria-describedby="set-backup-secrets-hint"
        />
        <p id="set-backup-secrets-hint" className="-mt-1.5 pl-6 text-xs text-muted">
          {secrets ? 'Keep this file somewhere private: anyone with it can use your keys.' : 'Keys are left out; you’d enter them again after restoring.'}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" className="h-10" leftIcon={<Download className="h-3.5 w-3.5" />} loading={downloading} onClick={() => void download()}>
            Download backup
          </Button>
          <Button variant="outline" className="h-10" leftIcon={<Upload className="h-3.5 w-3.5" />} onClick={() => fileRef.current?.click()}>
            Restore…
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            aria-hidden
            tabIndex={-1}
            onChange={(e) => void pick(e.target.files?.[0])}
          />
        </div>
      </div>
      <ConfirmDialog
        open={!!pending}
        onClose={() => setPending(null)}
        onConfirm={() => void restore()}
        loading={restoring}
        title="Restore these settings?"
        description="Your current settings are replaced by the ones in the file. Watch folders with the same path are updated, others are added. Keys left out of the file stay as they are."
        confirmLabel="Restore"
      >
        {pending && (
          <p className="break-all font-mono text-xs text-muted">
            {pending.name}
            {pending.created && ` · made ${new Date(pending.created).toLocaleString()}`}
          </p>
        )}
      </ConfirmDialog>
    </Row>
  );
}

/* ------------------------------------------------------------------ about */

function AboutRow() {
  const health = useHealth();
  const { data: auth } = useAuthStatus();
  const commit = health.data?.commit ? health.data.commit.slice(0, 7) : null;
  const version = `${health.data?.version ?? auth?.version ?? '–'}${commit ? ` (${commit})` : ''}`;
  const ok = health.data?.status === 'ok';
  const tone = health.isPending ? 'neutral' : health.isError ? 'danger' : ok ? 'success' : 'warning';
  const status = health.isPending
    ? 'Checking…'
    : health.isError
      ? `Version ${version} · can’t reach the server`
      : `Version ${version} · server ${ok ? 'healthy' : (health.data?.status ?? 'unknown')}`;
  return (
    <Row id="set-about" group label="About" help="Schema version, the commit it was built from, and server health.">
      <ConnStatus id="set-about" tone={tone} status={status}>
        <Button
          variant="outline"
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          loading={health.isFetching}
          onClick={() => void health.refetch()}
        >
          Check again
        </Button>
      </ConnStatus>
    </Row>
  );
}

function DemoRow() {
  const reset = useDemoReset();
  const [confirm, setConfirm] = useState(false);
  return (
    <Row id="set-demo" group label="Demo library" help="Rebuild the made-up demo folders and the “Demo import” job.">
      <Button variant="danger" className="h-10" leftIcon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => setConfirm(true)}>
        Reset demo
      </Button>
      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => reset.mutate(undefined, { onSettled: () => setConfirm(false) })}
        loading={reset.isPending}
        tone="danger"
        title="Reset the demo library?"
        description="Demo folders are recreated from scratch. Only files under the demo directory are affected."
        confirmLabel="Reset demo"
      />
    </Row>
  );
}
