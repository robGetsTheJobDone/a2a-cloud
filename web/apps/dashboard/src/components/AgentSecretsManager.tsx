import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  deleteAgentSecret,
  listAgentSecrets,
  upsertAgentSecret,
  type AgentSecret,
} from "../api";
import {
  EmptyState,
  FormField,
  InlineAlert,
  LiveRegion,
  LoadingState,
  SurfacePanel,
  TextInput,
  ToolbarButton,
} from "./DashboardChrome";

const RESERVED_SECRET_KEYS = new Set(["PORT", "HOST", "PYTHONPATH"]);
const RESERVED_SECRET_PREFIXES = ["A2A_", "KUBERNETES_"];
const MASKED_PLACEHOLDER = "********";

export type AgentSecretsManagerProps = {
  agentName: string;
  title?: string;
  description?: ReactNode;
  className?: string;
};

export function AgentSecretsManager({
  agentName,
  title = "Environment secrets",
  description = "Values are projected into this agent runtime and redacted after save.",
  className,
}: AgentSecretsManagerProps) {
  const keyInputId = useId();
  const keyHelpId = useId();
  const keyErrorId = useId();
  const valueInputId = useId();
  const valueHelpId = useId();
  const formErrorId = useId();
  const listStatusId = useId();

  const [secrets, setSecrets] = useState<AgentSecret[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [confirmDeleteKey, setConfirmDeleteKey] = useState<string | null>(null);
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [refreshNonce, setRefreshNonce] = useState(0);

  const normalizedKey = key.trim();
  const keyValidationError = validateAgentSecretKey(normalizedKey);
  const visibleKeyError =
    normalizedKey || submitAttempted ? keyValidationError : null;
  const valueError =
    submitAttempted && value.length === 0 ? "Secret value is required." : null;
  const replacesExisting = Boolean(
    secrets?.some((secret) => secret.key === normalizedKey),
  );
  const busy = saving || deletingKey !== null;
  const canSubmit =
    normalizedKey.length > 0 &&
    value.length > 0 &&
    !keyValidationError &&
    !busy &&
    !loading;
  const sortedSecrets = useMemo(() => sortSecrets(secrets || []), [secrets]);

  const refresh = useCallback(() => {
    setRefreshNonce((nonce) => nonce + 1);
  }, []);

  useEffect(() => {
    let active = true;

    setLoading(true);
    setLoadError(null);
    setConfirmDeleteKey(null);

    listAgentSecrets(agentName)
      .then((nextSecrets) => {
        if (!active) return;
        setSecrets(sortSecrets(nextSecrets));
      })
      .catch((ex) => {
        if (!active) return;
        setLoadError(errorMessage(ex));
        setSecrets(null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [agentName, refreshNonce]);

  async function saveSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitAttempted(true);

    const nextKeyError = validateAgentSecretKey(normalizedKey);
    if (nextKeyError || value.length === 0) {
      return;
    }

    setSaving(true);
    setActionError(null);
    try {
      const saved = await upsertAgentSecret(agentName, {
        key: normalizedKey,
        value,
      });
      setSecrets((current) => upsertSecret(current || [], saved));
      setLoadError(null);
      setKey("");
      setValue("");
      setShowValue(false);
      setSubmitAttempted(false);
      setConfirmDeleteKey(null);
    } catch (ex) {
      setActionError(errorMessage(ex));
    } finally {
      setSaving(false);
    }
  }

  async function deleteSecret(secretKey: string) {
    if (deletingKey) return;

    setDeletingKey(secretKey);
    setActionError(null);
    try {
      await deleteAgentSecret(agentName, secretKey);
      setSecrets((current) =>
        sortSecrets((current || []).filter((secret) => secret.key !== secretKey)),
      );
      setConfirmDeleteKey(null);
    } catch (ex) {
      setActionError(errorMessage(ex));
    } finally {
      setDeletingKey(null);
    }
  }

  return (
    <section
      className={className}
      aria-labelledby={`${listStatusId}-title`}
      aria-describedby={description ? `${listStatusId}-description` : undefined}
    >
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 pb-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h2
            id={`${listStatusId}-title`}
            className="text-sm font-semibold text-ink"
          >
            {title}
          </h2>
          {description && (
            <p
              id={`${listStatusId}-description`}
              className="mt-1 max-w-lg text-xs leading-relaxed text-ink-muted"
            >
              {description}
            </p>
          )}
          <div className="mt-2 text-[11px] uppercase text-ink-faint">
            {loading
              ? "Loading secrets"
              : `${sortedSecrets.length} configured`}
          </div>
        </div>

        <ToolbarButton
          onClick={refresh}
          disabled={loading || busy}
          variant="secondary"
          size="sm"
        >
          Refresh
        </ToolbarButton>
      </div>

      <form
        onSubmit={saveSecret}
        className="mt-4 grid gap-3 lg:grid-cols-[minmax(180px,220px)_minmax(240px,1fr)_auto]"
      >
        <FormField label="Key">
          <TextInput
            id={keyInputId}
            value={key}
            onChange={(event) => {
              setKey(event.target.value);
              setActionError(null);
            }}
            placeholder="SECRET_KEY"
            autoComplete="off"
            spellCheck={false}
            disabled={busy || loading}
            aria-invalid={Boolean(visibleKeyError)}
            aria-describedby={[
              keyHelpId,
              visibleKeyError ? keyErrorId : null,
            ]
              .filter(Boolean)
              .join(" ")}
            attention={Boolean(visibleKeyError)}
            mono
          />
          <span
            id={keyHelpId}
            className="mt-1 block text-[11px] leading-relaxed text-ink-faint"
          >
            Use env-var syntax. Reserved: PORT, HOST, PYTHONPATH, A2A_*,
            KUBERNETES_*.
          </span>
        </FormField>

        <FormField label="Value">
          <div className="flex min-w-0 gap-2">
            <TextInput
              id={valueInputId}
              value={value}
              onChange={(event) => {
                setValue(event.target.value);
                setActionError(null);
              }}
              placeholder="value"
              type={showValue ? "text" : "password"}
              autoComplete="new-password"
              disabled={busy || loading}
              aria-invalid={Boolean(valueError)}
              aria-describedby={[
                valueHelpId,
                valueError ? formErrorId : null,
              ]
                .filter(Boolean)
                .join(" ")}
              attention={Boolean(valueError)}
              className="min-w-0 flex-1"
            />
            <ToolbarButton
              type="button"
              onClick={() => setShowValue((visible) => !visible)}
              aria-pressed={showValue}
              disabled={busy || loading || value.length === 0}
              size="md"
            >
              {showValue ? "Hide" : "Show"}
            </ToolbarButton>
          </div>
          <span
            id={valueHelpId}
            className="mt-1 block text-[11px] leading-relaxed text-ink-faint"
          >
            Values are masked in the form and never returned in plaintext.
          </span>
        </FormField>

        <div className="flex items-start lg:pt-5">
          <ToolbarButton
            type="submit"
            disabled={!canSubmit}
            variant="primary"
            size="md"
            className="w-full lg:w-auto"
          >
            {saving ? "Saving..." : replacesExisting ? "Replace" : "Save"}
          </ToolbarButton>
        </div>
      </form>

      {visibleKeyError && (
        <InlineAlert
          id={keyErrorId}
          tone="amber"
          className="mt-3 text-xs"
        >
          {visibleKeyError}
        </InlineAlert>
      )}

      {valueError && (
        <InlineAlert
          id={formErrorId}
          tone="amber"
          className="mt-3 text-xs"
        >
          {valueError}
        </InlineAlert>
      )}

      {actionError && (
        <div className="mt-3">
          <InlineAlert tone="red">{actionError}</InlineAlert>
        </div>
      )}

      <LiveRegion>
        {listStatusText(loading, loadError, sortedSecrets.length)}
      </LiveRegion>

      <div className="mt-4" id={listStatusId}>
        {loading ? (
          <LoadingState label="Loading secrets..." />
        ) : loadError ? (
          <div className="space-y-3">
            <InlineAlert tone="red">{loadError}</InlineAlert>
            <ToolbarButton onClick={refresh} variant="secondary" size="sm">
              Retry
            </ToolbarButton>
          </div>
        ) : sortedSecrets.length === 0 ? (
          <EmptyState
            title="No secrets configured"
            description="Add a key and value above. Saved values appear here masked after they are stored."
          />
        ) : (
          <ul className="grid gap-2 md:grid-cols-2">
            {sortedSecrets.map((secret) => {
              const confirming = confirmDeleteKey === secret.key;
              const deleting = deletingKey === secret.key;

              return (
                <SurfacePanel
                  as="li"
                  key={secret.key}
                  className="p-3 text-xs"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-mono text-ink">
                        {secret.key}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-ink-faint">
                        <span
                          className="rounded-md border border-runtime-line-soft/60 bg-runtime-bg px-1.5 py-0.5 font-mono text-ink-dim"
                          aria-label={`${secret.key} value is masked`}
                        >
                          {maskedValue(secret.value_redacted)}
                        </span>
                        <span>Updated {fmtDate(secret.updated_at)}</span>
                      </div>
                    </div>

                    {!confirming && (
                      <ToolbarButton
                        onClick={() => setConfirmDeleteKey(secret.key)}
                        disabled={deletingKey !== null}
                        variant="danger"
                        size="xs"
                      >
                        Delete
                      </ToolbarButton>
                    )}
                  </div>

                  {confirming && (
                    <div
                      className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-runtime-line-soft/60 pt-3"
                      role="group"
                      aria-label={`Confirm deleting ${secret.key}`}
                    >
                      <span className="mr-auto text-[11px] text-signal-danger/80">
                        Delete this secret?
                      </span>
                      <ToolbarButton
                        onClick={() => setConfirmDeleteKey(null)}
                        disabled={deleting}
                        variant="ghost"
                        size="xs"
                      >
                        Cancel
                      </ToolbarButton>
                      <ToolbarButton
                        onClick={() => deleteSecret(secret.key)}
                        disabled={deleting}
                        variant="danger"
                        size="xs"
                      >
                        {deleting ? "Deleting..." : "Confirm"}
                      </ToolbarButton>
                    </div>
                  )}
                </SurfacePanel>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

function validateAgentSecretKey(key: string): string | null {
  if (!key) return "Secret key is required.";
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key)) {
    return "Use environment variable syntax: letters, numbers, underscores, and no leading number.";
  }

  const upperKey = key.toUpperCase();
  if (
    RESERVED_SECRET_KEYS.has(upperKey) ||
    RESERVED_SECRET_PREFIXES.some((prefix) => upperKey.startsWith(prefix))
  ) {
    return "That key is reserved by the platform runtime.";
  }

  return null;
}

function upsertSecret(secrets: AgentSecret[], next: AgentSecret): AgentSecret[] {
  const exists = secrets.some((secret) => secret.key === next.key);
  if (!exists) return sortSecrets([...secrets, next]);
  return sortSecrets(
    secrets.map((secret) => (secret.key === next.key ? next : secret)),
  );
}

function sortSecrets(secrets: AgentSecret[]): AgentSecret[] {
  return [...secrets].sort((left, right) => left.key.localeCompare(right.key));
}

function maskedValue(redactedValue: string): string {
  const value = redactedValue.trim();
  return value.length > 0 ? value : MASKED_PLACEHOLDER;
}

function listStatusText(
  loading: boolean,
  loadError: string | null,
  count: number,
): string {
  if (loading) return "Loading agent secrets.";
  if (loadError) return `Agent secrets failed to load. ${loadError}`;
  if (count === 0) return "No agent secrets configured.";
  return `${count} agent secret${count === 1 ? "" : "s"} configured.`;
}

function fmtDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function errorMessage(ex: unknown): string {
  return ex instanceof Error ? ex.message : String(ex);
}
