import { useState, type FormEvent, type ReactNode } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { Eye, EyeOff, LogIn, ShieldCheck } from 'lucide-react';
import { useAuthStatus, useLogin, useSetup } from '@/api/hooks';
import { errorMessage, isApiError } from '@/api/client';
import { Button, Field, IconButton, Input } from '@/components/ui';
import { Wordmark } from '@/components/brand/Mark';
import { safeNext } from './safeNext';

/** Full-bleed brand layout for /setup and /login (SPEC §9.2). The brand panel stays dark in both themes. */
export function BrandLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-[100dvh] flex-col bg-ink-950 md:flex-row">
      <div className="relative flex items-center justify-center overflow-hidden bg-[#0F1216] px-6 py-10 md:w-1/2 md:py-0">
        <div
          className="pointer-events-none absolute left-1/2 top-1/2 h-[420px] w-[420px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand-gradient opacity-[0.16] blur-3xl animate-glow"
          aria-hidden
        />
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.07]"
          style={{
            backgroundImage:
              'radial-gradient(circle at 1px 1px, rgba(214,237,243,0.9) 1px, transparent 0)',
            backgroundSize: '24px 24px',
          }}
          aria-hidden
        />
        <div className="relative flex flex-col items-center gap-6 text-center">
          <Wordmark forceDark width={260} className="h-auto w-[180px] md:w-[260px]" />
          <p className="hidden max-w-xs text-[13px] text-[#8B95A3] md:block">
            Preview every rename before anything moves. Movies, series and anime, matched across TMDb, TheTVDB,
            AniDB, Trakt and TVmaze.
          </p>
        </div>
      </div>
      <div className="flex flex-1 items-start justify-center px-4 py-8 md:items-center md:px-10">
        <div className="w-full max-w-sm animate-slide-up">{children}</div>
      </div>
    </div>
  );
}

export function PasswordInput(props: {
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  id?: string;
  invalid?: boolean;
  'aria-describedby'?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <Input
      id={props.id}
      type={show ? 'text' : 'password'}
      value={props.value}
      onChange={(e) => props.onChange(e.target.value)}
      autoComplete={props.autoComplete}
      invalid={props.invalid}
      aria-describedby={props['aria-describedby']}
      required
      minLength={8}
      rightSlot={
        <IconButton
          label={show ? 'Hide password' : 'Show password'}
          icon={show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          size="xs"
          tooltip={false}
          onClick={() => setShow((s) => !s)}
        />
      }
    />
  );
}

export function SetupPage() {
  const { data } = useAuthStatus();
  const setup = useSetup();
  const navigate = useNavigate();
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [setupCode, setSetupCode] = useState('');
  const [touched, setTouched] = useState(false);

  if (data && !data.setup_required) return <Navigate to="/" replace />;
  const needsCode = Boolean(data?.setup_token_required);

  const tooShort = password.length > 0 && password.length < 8;
  const mismatch = confirm.length > 0 && confirm !== password;
  const userInvalid = username.trim().length < 3;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (tooShort || mismatch || userInvalid || password.length < 8 || (needsCode && !setupCode.trim())) return;
    setup.mutate(
      { username: username.trim(), password, ...(needsCode ? { setup_token: setupCode.trim() } : {}) },
      { onSuccess: () => navigate('/', { replace: true }) },
    );
  };

  return (
    <BrandLayout>
      <form onSubmit={submit} className="flex flex-col gap-5" noValidate>
        <div>
          <div className="mb-3 inline-flex h-9 w-9 items-center justify-center rounded-lg bg-brand-sky/10 text-accent">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Welcome to Schema</h1>
          <p className="mt-1 text-[13px] text-muted">Create the administrator account. You can change it later.</p>
        </div>
        {needsCode && (
          <Field
            label="Setup code"
            hint="Printed in the container log: run docker logs schema on the NAS."
            error={touched && !setupCode.trim() ? 'Required' : undefined}
          >
            <Input
              value={setupCode}
              onChange={(e) => setSetupCode(e.target.value)}
              autoComplete="one-time-code"
              spellCheck={false}
              required
            />
          </Field>
        )}
        <Field label="Username" error={touched && userInvalid ? 'At least 3 characters' : undefined}>
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
            minLength={3}
            maxLength={64}
            data-autofocus
            autoFocus
          />
        </Field>
        <Field
          label="Password"
          hint="At least 8 characters."
          error={touched && (tooShort || password.length === 0) ? 'At least 8 characters' : undefined}
        >
          <PasswordInput value={password} onChange={setPassword} autoComplete="new-password" invalid={touched && tooShort} />
        </Field>
        <Field label="Confirm password" error={mismatch ? 'Passwords do not match' : undefined}>
          <PasswordInput value={confirm} onChange={setConfirm} autoComplete="new-password" invalid={mismatch} />
        </Field>
        {setup.isError && (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            {errorMessage(setup.error)}
          </p>
        )}
        <Button type="submit" variant="primary" size="lg" loading={setup.isPending} block>
          Create account
        </Button>
        {data?.demo && <p className="text-center text-xs text-muted">Demo mode: matching uses an offline sample catalogue.</p>}
      </form>
    </BrandLayout>
  );
}

export function LoginPage() {
  const { data } = useAuthStatus();
  const login = useLogin();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const next = safeNext(params.get('next'));

  if (data?.setup_required) return <Navigate to="/setup" replace />;
  if (data && (!data.auth_enabled || data.authenticated)) return <Navigate to={next} replace />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password) return;
    login.mutate({ username, password }, { onSuccess: () => navigate(next, { replace: true }) });
  };

  return (
    <BrandLayout>
      <form onSubmit={submit} className="flex flex-col gap-5">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
          <p className="mt-1 text-[13px] text-muted">Use the administrator account for this Schema server.</p>
        </div>
        <Field label="Username">
          <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required autoFocus />
        </Field>
        <Field label="Password">
          <PasswordInput value={password} onChange={setPassword} autoComplete="current-password" />
        </Field>
        {login.isError && (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            {isApiError(login.error) && login.error.isUnauthorized ? 'Wrong username or password.' : errorMessage(login.error)}
          </p>
        )}
        <Button
          type="submit"
          variant="primary"
          size="lg"
          loading={login.isPending}
          leftIcon={<LogIn className="h-4 w-4" />}
          block
        >
          Sign in
        </Button>
        {data?.version && <p className="text-center text-2xs text-muted">Schema {data.version}</p>}
      </form>
    </BrandLayout>
  );
}