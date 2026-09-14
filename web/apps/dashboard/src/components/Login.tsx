import { useEffect, useState } from "react";
import { trackEvent } from "../analytics";
import { startOidcLogin, type Session } from "../api";
import { captureStudioClaimFromHash, captureStudioPrefillFromHash } from "../studioPrefill";
import { InlineAlert, TextInput, ToolbarButton } from "./DashboardChrome";
import { Icon } from "./Icon";

type Props = { onLogin: (s: Session) => void; initialError?: string | null };

export function Login({ initialError }: Props) {
  const [email, setEmail] = useState("");
  const [oidcBusy, setOidcBusy] = useState(false);
  const [ssoBusy, setSsoBusy] = useState(false);
  const [err, setErr] = useState<string | null>(initialError || null);

  useEffect(() => {
    captureStudioClaimFromHash();
    captureStudioPrefillFromHash();
  }, []);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    submitOidc();
  }

  function submitSso() {
    const normalizedEmail = normalizeEmail(email);
    setEmail(normalizedEmail);
    if (!isLikelyEmail(normalizedEmail)) {
      setErr("Enter your work email to continue with SSO.");
      return;
    }
    setSsoBusy(true);
    setErr(null);
    trackEvent("auth_sso_started");
    startOidcLogin(currentRedirectPath(), normalizedEmail);
  }

  function submitOidc() {
    const normalizedEmail = normalizeEmail(email);
    setEmail(normalizedEmail);
    if (!isLikelyEmail(normalizedEmail)) {
      setErr("Enter a valid email.");
      return;
    }
    setOidcBusy(true);
    setErr(null);
    trackEvent("auth_oidc_started");
    startOidcLogin(currentRedirectPath(), normalizedEmail);
  }

  const anyBusy = oidcBusy || ssoBusy;

  return (
    <div className="min-h-app-shell relative flex items-center justify-center overflow-hidden bg-runtime-bg px-4 py-8 text-ink">
      <form
        onSubmit={submit}
        className="relative w-full max-w-[420px] rounded-2xl border border-runtime-line-soft/80 bg-runtime-panel/70 p-5 shadow-2xl shadow-black/50 ring-1 ring-white/5 backdrop-blur-xl sm:p-8"
      >
        <img
          src="/brand/logo-lockup.svg"
          alt="a2a"
          className="mb-6 h-9 w-auto sm:mb-8"
        />
        <h1 className="text-2xl font-semibold text-ink">
          Sign in
        </h1>
        <p className="mt-2 text-sm text-ink-dim">
          Use your a2a Cloud account.
        </p>

        {err && (
          <InlineAlert tone="red" role="alert" className="mt-6">
            {err}
          </InlineAlert>
        )}

        <label className="mb-3 mt-6 block">
          <span className="mb-1.5 block text-[11px] font-medium uppercase text-ink-muted">
            Email
          </span>
          <span className="relative block">
            <Icon
              name="user"
              size={17}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted"
            />
            <TextInput
              type="email"
              required
              autoFocus
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="h-11 rounded-lg border-runtime-line/80 bg-runtime-bg/60 pl-10 pr-3 focus:bg-runtime-bg"
              placeholder="name@company.com"
            />
          </span>
        </label>

        <ToolbarButton
          type="submit"
          disabled={anyBusy}
          variant="primary"
          size="md"
          className="mt-4 h-11 w-full text-sm"
        >
          {oidcBusy ? "Opening..." : "Continue with a2a Cloud"}
          {!oidcBusy && <Icon name="arrow-right" size={17} />}
        </ToolbarButton>

        <ToolbarButton
          type="button"
          onClick={submitSso}
          disabled={anyBusy || !email.trim()}
          size="md"
          className="mt-3 h-11 w-full text-sm"
        >
          {ssoBusy ? "Opening..." : "Enterprise SSO"}
          {!ssoBusy && <Icon name="shield" size={17} />}
        </ToolbarButton>
      </form>
    </div>
  );
}

function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}

function isLikelyEmail(value: string): boolean {
  const at = value.lastIndexOf("@");
  if (at <= 0 || at !== value.indexOf("@") || at >= value.length - 1) return false;
  return value.slice(at + 1).includes(".");
}

function currentRedirectPath(): string {
  const path = `${window.location.pathname}${window.location.search}`;
  return path === "/" ? "/workspace" : path;
}
