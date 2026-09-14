import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  deleteLlmCreds,
  listLlmModelCatalog,
  listLlmCreds,
  upsertLlmCreds,
  type LLMCreds,
  type LLMModelCatalog,
  type LLMModelCatalogModel,
  type LLMModelCatalogProvider,
} from "../api";
import {
  CodeBlock,
  CopyButton,
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  PersistentRoutePanel,
  SectionPanel,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceLink,
  SelectInput,
  SummaryMetric,
  TextArea,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
} from "./DashboardSectionCache";
import { llmKeyViewForPath } from "../navigation";
import { RoutePageShell } from "./RoutePageShell";
import { DetailSheet } from "./ListDetailLayout";
import { DashboardSurfacePosture } from "./SurfacePosture";

// Provider presets are no longer hardcoded here — the whole catalog (providers,
// base URLs, key env vars, models) is generated from the litellm package and
// served by the control plane at /v1/me/llm-creds/catalog. See
// control-plane/tools/gen_litellm_providers.py.

type TemperatureMode = "omit" | "default" | "custom";
type LlmKeyDetailSection = "overview" | "endpoint" | "options" | "usage";

const LLM_KEY_DETAIL_SECTIONS: Array<{
  id: LlmKeyDetailSection;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Credential identity, model, redacted key, temperature, and timestamps.",
  },
  {
    id: "endpoint",
    label: "Endpoint",
    description: "OpenAI-compatible base URL sent through LiteLLM.",
  },
  {
    id: "options",
    label: "Provider options",
    description: "Provider-specific JSON request fields attached to calls.",
  },
  {
    id: "usage",
    label: "Usage & security",
    description: "Where this credential is used, fallback guidance, and delete controls.",
  },
];

function normalizeLlmKeyDetailSection(
  value: string | null | undefined,
): LlmKeyDetailSection {
  return LLM_KEY_DETAIL_SECTIONS.some((section) => section.id === value)
    ? (value as LlmKeyDetailSection)
    : "overview";
}

const CUSTOM_MODEL_VALUE = "__custom__";

const KIMI_THINKING_DISABLED = JSON.stringify(
  { thinking: { type: "disabled" } },
  null,
  2,
);

function llmKeyDetailRoute(
  name: string,
  section: LlmKeyDetailSection = "overview",
) {
  const suffix = section === "overview" ? "" : `/${section}`;
  return `/llm-keys/saved/${encodeURIComponent(name)}${suffix}`;
}

export function LlmKeys() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { credName: routeCredName, section: routeSection } = useParams<{
    credName?: string;
    section?: string;
  }>();
  const view = llmKeyViewForPath(pathname);
  const detailSection = normalizeLlmKeyDetailSection(routeSection);
  const {
    data: creds,
    error: loadErr,
    refresh: refreshCreds,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.settings.llmKeyCreds,
    listLlmCreds,
  );
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deletingName, setDeletingName] = useState<string | null>(null);
  const err = actionErr ?? loadErr;

  const refresh = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshCreds();
      setConfirmDelete(null);
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshCreds]);

  const defaultCred = creds?.find((item) => item.name === "default") || null;
  const providerCount = new Set((creds || []).map((item) => providerLabel(item.base_url))).size;
  const health = llmKeyHealth(creds, defaultCred);
  const selectedCred =
    view.id === "saved" && routeCredName
      ? creds?.find((item) => item.name === routeCredName) || null
      : null;

  return (
    <RoutePageShell
      routeId="keys"
      data-onboarding-target="llm-keys-page"
      actions={
        <ToolbarButton onClick={refresh} disabled={creds === null}>
          Refresh
        </ToolbarButton>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      <LlmKeyPosture
        creds={creds}
        defaultCred={defaultCred}
        health={health}
      />

      <PersistentRoutePanel active={view.id === "overview"}>
        <LlmKeysOverview
          savedCount={creds?.length || 0}
          providerCount={providerCount}
          defaultCred={defaultCred}
          health={health}
        />
      </PersistentRoutePanel>

      <PersistentRoutePanel active={view.id === "saved"}>
        <SavedKeysPanel creds={creds} />
      </PersistentRoutePanel>

      {/* Mandate C: credential detail/edit opens in place as a right-side sheet
          layered over the saved-keys list, which stays mounted behind it. The
          sheet is route-driven (open while /llm-keys/saved/:credName resolves)
          so deep links and back/forward keep working. */}
      <DetailSheet
        open={view.id === "saved" && Boolean(routeCredName)}
        onClose={() => navigate("/llm-keys/saved")}
        size="lg"
        title={
          <span className="break-all font-mono">
            {selectedCred?.name ?? routeCredName ?? "Credential"}
          </span>
        }
        description={
          selectedCred ? providerLabel(selectedCred.base_url) : undefined
        }
      >
        <LlmKeyDetail
          cred={selectedCred}
          initialSection={detailSection}
          open={view.id === "saved" && Boolean(routeCredName)}
          loading={creds === null}
          requestedName={routeCredName ?? ""}
          confirmDelete={confirmDelete}
          deletingName={deletingName}
          onConfirmDelete={setConfirmDelete}
          onDeletingName={setDeletingName}
          onDeleted={async () => {
            await refresh();
            navigate("/llm-keys/saved");
          }}
          onError={setActionErr}
        />
      </DetailSheet>

      {/* Mandate C: add/replace opens in place as a right-side sheet layered over
          the saved-keys list, never a full-page route swap that unmounts it. */}
      <DetailSheet
        open={view.id === "add"}
        onClose={() => navigate("/llm-keys/saved")}
        size="lg"
        title="Add or replace key"
        description="Choose a provider preset, then override the model or base URL if needed."
      >
        <div data-onboarding-target="llm-keys-form">
          <AddForm
            onAdded={async () => {
              await refresh();
              navigate("/llm-keys/saved");
            }}
          />
        </div>
      </DetailSheet>
    </RoutePageShell>
  );
}

function LlmKeyPosture({
  creds,
  defaultCred,
  health,
}: {
  creds: LLMCreds[] | null;
  defaultCred: LLMCreds | null;
  health: ReturnType<typeof llmKeyHealth>;
}) {
  const savedCount = creds?.length ?? null;
  const nextAction = credentialNextAction(creds, defaultCred);
  const hasDefaultOptions = Boolean(defaultCred && hasExtraBody(defaultCred.extra_body));

  return (
    <DashboardSurfacePosture
      data-onboarding-target="llm-keys-posture"
      eyebrow="Credential posture"
      title="Provider credentials via LiteLLM"
      status={{
        label: health.label,
        tone: healthPillTone(health.tone),
        dot: health.tone === "emerald",
        pulse: health.tone === "emerald",
      }}
      metrics={[
        {
          label: "saved",
          value: savedCount === null ? "..." : savedCount.toLocaleString(),
        },
        {
          label: "default",
          value: defaultCred ? defaultCred.name : "missing",
          tone: defaultCred ? "live" : "authority",
        },
        {
          label: "model",
          value: defaultCred?.model || "-",
        },
        {
          label: "options",
          value: hasDefaultOptions ? "custom" : "standard",
          tone: hasDefaultOptions ? "authority" : "neutral",
        },
      ]}
      actions={
        <>
          <ToolbarLink href="/llm-keys/saved" size="sm">
            Saved keys
          </ToolbarLink>
          <ToolbarLink
            href={nextAction.href}
            variant={nextAction.tone === "primary" ? "primary" : "secondary"}
            size="sm"
          >
            {nextAction.action}
          </ToolbarLink>
        </>
      }
    />
  );
}

function healthPillTone(
  tone: ReturnType<typeof llmKeyHealth>["tone"],
): "live" | "authority" | "neutral" {
  if (tone === "emerald") return "live";
  if (tone === "amber") return "authority";
  return "neutral";
}

function LlmKeysOverview({
  savedCount,
  providerCount,
  defaultCred,
  health,
}: {
  savedCount: number;
  providerCount: number;
  defaultCred: LLMCreds | null;
  health: ReturnType<typeof llmKeyHealth>;
}) {
  const nextAction = credentialNextActionFromState(savedCount, defaultCred);
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(300px,380px)]">
      <div className="grid gap-3 md:grid-cols-2">
        <SelectableSurfaceLink
          href="/llm-keys/saved"
          className="p-4 hover:border-runtime-line-strong hover:bg-runtime-panel/70"
        >
          <div className="text-[10px] uppercase tracking-wider text-ink-faint">
            Saved keys
          </div>
          <div className="mt-2 text-lg font-semibold text-ink">
            {savedCount.toLocaleString()}
          </div>
          <div className="mt-1 truncate text-xs text-ink-muted">
            {providerCount} provider{providerCount === 1 ? "" : "s"} configured
          </div>
        </SelectableSurfaceLink>
        <SelectableSurfaceLink
          href="/llm-keys/add"
          className="p-4 hover:border-runtime-line-strong hover:bg-runtime-panel/70"
        >
          <div className="text-[10px] uppercase tracking-wider text-ink-faint">
            Add or replace
          </div>
          <div className="mt-2 text-lg font-semibold text-ink">
            {defaultCred ? "Default exists" : "Default missing"}
          </div>
          <div className="mt-1 truncate text-xs text-ink-muted">
            {defaultCred ? defaultCred.model : "Create a default fallback key"}
          </div>
        </SelectableSurfaceLink>
      </div>

      <aside className="min-w-0">
        <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
          Setup path
        </div>
        <h2 className="mt-2 text-base font-semibold text-ink">
          Make model access reliable
        </h2>
        <div className="mt-4 grid gap-2">
          <CredentialActionLink
            href={nextAction.href}
            label={nextAction.label}
            detail={nextAction.detail}
            tone={nextAction.tone === "primary" ? "amber" : "neutral"}
          />
          <CredentialActionLink
            href="/llm-keys/saved"
            label="Review saved credentials"
            detail={`${savedCount.toLocaleString()} key${savedCount === 1 ? "" : "s"} across ${providerCount.toLocaleString()} provider${providerCount === 1 ? "" : "s"}.`}
            tone={savedCount > 0 ? "emerald" : "amber"}
          />
          <CredentialActionLink
            href="/llm-keys/add"
            label="Replace or rotate a key"
            detail="Use the same credential name to update a redacted provider secret."
          />
        </div>
        {health.tone !== "emerald" && (
          <InlineAlert tone="amber" className="mt-4">
            {health.detail}
          </InlineAlert>
        )}
      </aside>
    </div>
  );
}

function CredentialActionLink({
  href,
  label,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  detail: string;
  tone?: "neutral" | "emerald" | "amber";
}) {
  const dotClassName =
    tone === "emerald"
      ? "bg-signal-live"
      : tone === "amber"
        ? "bg-signal-authority"
        : "bg-ink-faint";

  return (
    <SelectableSurfaceLink href={href} className="p-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dotClassName}`}
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-ink">
            {label}
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-ink-muted">
            {detail}
          </span>
        </span>
      </div>
    </SelectableSurfaceLink>
  );
}

function SavedKeysPanel({
  creds,
}: {
  creds: LLMCreds[] | null;
}) {
  return (
    <div data-onboarding-target="llm-keys-list">
      <SectionPanel
        title="Saved keys"
        description="Saved provider credentials available to chat and agent runs. The default key is used when no named key is selected."
      >
        {creds === null ? (
          <LoadingState label="Loading LLM keys..." />
        ) : creds.length === 0 ? (
          <EmptyState
            title="No LLM keys saved"
            description={
              <>
                Add one on the Add key page. The entry named <span className="font-mono text-ink-soft">default</span> is used unless another key is selected from chat.
              </>
            }
          />
        ) : (
          <ul className="space-y-3">
            {creds.map((c) => (
              <li key={c.id}>
                <SelectableSurfaceLink
                  href={llmKeyDetailRoute(c.name)}
                  className="flex flex-col gap-3 bg-runtime-bg/70 px-3 py-3 sm:flex-row sm:items-start sm:justify-between"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-sm text-ink">
                        {c.name}
                      </span>
                      {c.name === "default" && (
                        <StatusBadge tone="neutral">
                          default
                        </StatusBadge>
                      )}
                      <StatusBadge tone="emerald" dot>
                        configured
                      </StatusBadge>
                      {hasExtraBody(c.extra_body) && (
                        <StatusBadge tone="amber">extra body</StatusBadge>
                      )}
                    </div>
                    <div className="mt-2 grid min-w-0 gap-2 text-xs text-ink-muted lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.8fr)]">
                      <ReadOnlyFact label="provider" value={providerLabel(c.base_url)} />
                      <ReadOnlyFact label="model" value={c.model} mono />
                      <ReadOnlyFact label="key" value={c.api_key_redacted} mono />
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-faint">
                      <span>{temperatureLabel(c.temperature_mode, c.temperature)}</span>
                      <span className="min-w-0 truncate">{c.base_url}</span>
                    </div>
                  </div>
                  <span className="shrink-0 text-xs font-medium text-ink-dim">
                    Open
                  </span>
                </SelectableSurfaceLink>
              </li>
            ))}
          </ul>
        )}
      </SectionPanel>
    </div>
  );
}

function LlmKeyDetail({
  cred,
  initialSection,
  open,
  loading,
  requestedName,
  confirmDelete,
  deletingName,
  onConfirmDelete,
  onDeletingName,
  onDeleted,
  onError,
}: {
  cred: LLMCreds | null;
  initialSection: LlmKeyDetailSection;
  open: boolean;
  loading: boolean;
  requestedName: string;
  confirmDelete: string | null;
  deletingName: string | null;
  onConfirmDelete: (name: string | null) => void;
  onDeletingName: (name: string | null) => void;
  onDeleted: () => Promise<void>;
  onError: (message: string) => void;
}) {
  // Detail tabs live in local state inside the sheet body (mandate D): no route
  // params. Deep links to .../:credName/:section still hydrate the starting tab.
  const [activeSection, setActiveSection] =
    useState<LlmKeyDetailSection>(initialSection);
  useEffect(() => {
    if (open) setActiveSection(initialSection);
  }, [open, initialSection, requestedName]);

  if (loading) return <LoadingState label="Loading LLM key..." />;
  if (!cred) {
    return (
      <EmptyState
        title="LLM key not found"
        description={`${requestedName} is not in your saved credentials.`}
        action={<ToolbarLink href="/llm-keys/saved">Back to saved keys</ToolbarLink>}
      />
    );
  }

  const currentCred = cred;
  const deleting = deletingName === currentCred.name;
  const confirming = confirmDelete === currentCred.name;
  const activeDetail =
    LLM_KEY_DETAIL_SECTIONS.find((section) => section.id === activeSection) ||
    LLM_KEY_DETAIL_SECTIONS[0];

  async function remove() {
    if (!confirming) {
      onConfirmDelete(currentCred.name);
      return;
    }
    onDeletingName(currentCred.name);
    try {
      await deleteLlmCreds(currentCred.name);
      await onDeleted();
    } catch (ex) {
      onError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      onDeletingName(null);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone="emerald" dot>
          configured
        </StatusBadge>
        {currentCred.name === "default" && <StatusBadge tone="neutral">default</StatusBadge>}
        {hasExtraBody(currentCred.extra_body) && <StatusBadge tone="amber">extra body</StatusBadge>}
        <ToolbarLink href="/llm-keys/add" variant="primary" size="sm" className="ml-auto">
          Replace key
        </ToolbarLink>
      </div>

      <div className="space-y-2">
        <SegmentedControl
          role="tablist"
          aria-label={`${currentCred.name} credential detail sections`}
          className="flex flex-wrap gap-1 bg-runtime-panel/60"
        >
          {LLM_KEY_DETAIL_SECTIONS.map((section) => (
            <SegmentedButton
              key={section.id}
              onClick={() => setActiveSection(section.id)}
              selected={activeSection === section.id}
            >
              {section.label}
            </SegmentedButton>
          ))}
        </SegmentedControl>
        <div className="text-sm leading-relaxed text-ink-muted">
          {activeDetail.description}
        </div>
      </div>

      {activeSection === "overview" && (
        <LlmKeyOverviewPanel cred={currentCred} />
      )}
      {activeSection === "endpoint" && (
        <LlmKeyEndpointPanel cred={currentCred} />
      )}
      {activeSection === "options" && (
        <LlmKeyOptionsPanel cred={currentCred} />
      )}
      {activeSection === "usage" && (
        <LlmKeyUsagePanel
          cred={currentCred}
          confirming={confirming}
          deleting={deleting}
          deletingName={deletingName}
          onRemove={remove}
        />
      )}
    </div>
  );
}

function LlmKeyOverviewPanel({ cred }: { cred: LLMCreds }) {
  return (
    <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <ReadOnlyFact label="model" value={cred.model} mono />
      <SummaryMetric
        label="key"
        size="compact"
        mono
        value={
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="min-w-0 truncate" title={cred.api_key_redacted}>
              {cred.api_key_redacted}
            </span>
            <CopyButton
              value={cred.api_key_redacted}
              label="Copy redacted key"
              className="shrink-0"
            />
          </span>
        }
      />
      <ReadOnlyFact
        label="temperature"
        value={temperatureLabel(cred.temperature_mode, cred.temperature)}
      />
      <ReadOnlyFact label="created" value={formatCredentialDate(cred.created_at)} />
      <ReadOnlyFact label="updated" value={formatCredentialDate(cred.updated_at)} />
      <ReadOnlyFact label="id" value={String(cred.id)} mono />
    </div>
  );
}

function LlmKeyEndpointPanel({ cred }: { cred: LLMCreds }) {
  return (
    <div className="mt-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">Base URL</div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          The normalized OpenAI-compatible endpoint sent through LiteLLM.
        </p>
      </div>
      <CodeBlock className="text-xs">{cred.base_url}</CodeBlock>
    </div>
  );
}

function LlmKeyOptionsPanel({ cred }: { cred: LLMCreds }) {
  return (
    <div className="mt-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">Provider options</div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          Additional request body fields sent with chat and agent model calls.
        </p>
      </div>
      <CodeBlock className="max-h-[420px] text-xs">
        {JSON.stringify(cred.extra_body || {}, null, 2)}
      </CodeBlock>
    </div>
  );
}

function LlmKeyUsagePanel({
  cred,
  confirming,
  deleting,
  deletingName,
  onRemove,
}: {
  cred: LLMCreds;
  confirming: boolean;
  deleting: boolean;
  deletingName: string | null;
  onRemove: () => void;
}) {
  return (
    <div className="mt-4 divide-y divide-runtime-line-soft/70 rounded-lg border border-runtime-line-soft/70 bg-runtime-panel/25">
      <section className="p-4">
        <div className="text-xs uppercase text-ink-muted">Usage</div>
        <p className="mt-2 text-sm leading-relaxed text-ink-dim">
          Chat and LLM-capable agents can use this credential when selected in workspace settings or request defaults.
        </p>
        {cred.name !== "default" && (
          <InlineAlert tone="amber" className="mt-3 text-xs">
            The default fallback credential is named <span className="font-mono">default</span>.
          </InlineAlert>
        )}
      </section>

      <section className="p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="text-xs uppercase text-ink-muted">Delete credential</div>
            <p className="mt-2 text-sm leading-relaxed text-ink-dim">
              Removing this credential stops future chat and agent calls that reference this name.
            </p>
          </div>
          <ToolbarButton
            type="button"
            variant={confirming ? "danger" : "secondary"}
            disabled={deletingName !== null}
            onClick={onRemove}
          >
            {deleting ? "Deleting..." : confirming ? "Confirm delete" : "Delete"}
          </ToolbarButton>
        </div>
        {confirming && (
          <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
            Deleting this credential removes it from chat and future agent calls that reference this name.
          </InlineAlert>
        )}
      </section>
    </div>
  );
}

function AddForm({ onAdded }: { onAdded: () => Promise<void> }) {
  const [name, setName] = useState("default");
  const [catalog, setCatalog] = useState<LLMModelCatalog | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [providerId, setProviderId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [editBase, setEditBase] = useState(false);
  const [model, setModel] = useState("");
  const [temperatureMode, setTemperatureMode] = useState<TemperatureMode>("omit");
  const [temperature, setTemperature] = useState("1");
  const [maxTokens, setMaxTokens] = useState("");
  const [topP, setTopP] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [thinking, setThinking] = useState<"default" | "enabled" | "disabled">("default");
  const [extraBodyText, setExtraBodyText] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const providers = catalog?.providers ?? [];
  const provider = providers.find((item) => item.id === providerId) ?? null;
  const selectedModel = provider?.models.find((m) => m.model === model) ?? null;
  // Which option controls to show comes straight from litellm's supported params
  // for the chosen model. Unknown (custom-typed) models fall back to permissive.
  const supported = selectedModel?.supported_params ?? [];
  const knownModel = selectedModel !== null;
  const showTemperature = !knownModel || supported.includes("temperature");
  const showMaxTokens =
    supported.includes("max_tokens") || supported.includes("max_completion_tokens");
  const showTopP = supported.includes("top_p");
  const showReasoning = supported.includes("reasoning_effort");
  const showThinking = supported.includes("thinking");
  const modelChoice = provider ? modelSelectValue(model, provider) : CUSTOM_MODEL_VALUE;
  const showKimiThinkingShortcut = provider ? isKimiProvider(provider, model) : false;
  const knownBase = Boolean(provider?.base_url);
  const baseLocked = knownBase && !editBase;
  const validationError =
    credentialFormError(
      name, baseUrl, model, apiKey, temperatureMode, temperature, extraBodyText,
    ) ?? modelOptionsError(showMaxTokens, maxTokens, showTopP, topP);
  const visibleValidationError =
    validationError === "API key is required." ? null : validationError;

  useEffect(() => {
    listLlmModelCatalog()
      .then((c) => {
        setCatalog(c);
        setLoadError(false);
      })
      .catch(() => {
        setCatalog(null);
        setLoadError(true);
      });
  }, []);

  // Select the first provider once the litellm catalog arrives.
  useEffect(() => {
    if (providers.length > 0 && !provider) {
      setProviderId(providers[0].id);
    }
  }, [providers, provider]);

  // Prefill the form from the selected provider's litellm-derived defaults.
  useEffect(() => {
    if (!provider) return;
    setBaseUrl(provider.base_url);
    setEditBase(false);
    setModel(defaultModelForProvider(provider));
    setExtraBodyText(
      hasExtraBody(provider.extra_body)
        ? JSON.stringify(provider.extra_body, null, 2)
        : "",
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider?.id]);

  // Reset model-specific reasoning knobs when the model changes.
  useEffect(() => {
    setReasoningEffort("");
    setThinking("default");
  }, [model]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (validationError) {
      setErr(validationError);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      // Structured controls assemble extra_body; the raw JSON box (advanced)
      // is merged on top for anything the controls don't cover.
      const structured: Record<string, unknown> = {};
      if (showMaxTokens && maxTokens.trim()) structured.max_tokens = Number(maxTokens);
      if (showTopP && topP.trim()) structured.top_p = Number(topP);
      if (showReasoning && reasoningEffort) structured.reasoning_effort = reasoningEffort;
      if (showThinking && thinking !== "default") structured.thinking = { type: thinking };
      const extra_body = { ...structured, ...parseExtraBody(extraBodyText) };
      await upsertLlmCreds({
        name: name.trim(),
        base_url: normalizeBaseUrl(baseUrl),
        api_key: apiKey.trim(),
        model: model.trim(),
        temperature_mode: showTemperature ? temperatureMode : "omit",
        temperature:
          showTemperature && temperatureMode === "custom" ? Number(temperature) : null,
        extra_body,
      });
      setApiKey("");
      await onAdded();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <InlineAlert tone="red">
        Couldn't load the provider catalog from LiteLLM. Reload the page to try again.
      </InlineAlert>
    );
  }
  if (!catalog) {
    return <LoadingState label="Loading LiteLLM providers..." />;
  }
  if (!provider) {
    return <InlineAlert tone="amber">No LLM providers are available.</InlineAlert>;
  }

  return (
    <form onSubmit={submit}>
        <div className="grid gap-3 md:grid-cols-2">
          <FormField
            label="Provider"
            description={`${providers.length} providers generated from LiteLLM.`}
          >
            <SelectInput
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.models.length > 0 ? `${p.label} · ${p.models.length} models` : p.label}
                </option>
              ))}
            </SelectInput>
          </FormField>
          <FormField label="Name">
            <TextInput
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="default"
              mono
            />
          </FormField>
          <div className="md:col-span-2">
            <FormField
              label="Model"
              description={
                provider.models.length > 0
                  ? `${provider.models.length} models from LiteLLM's catalog for ${provider.label}.`
                  : "No catalog models for this provider — enter the model name LiteLLM expects."
              }
            >
              <SelectInput
                value={modelChoice}
                onChange={(e) => {
                  const next = e.target.value;
                  setModel(next === CUSTOM_MODEL_VALUE ? "" : next);
                }}
                className="font-mono"
              >
                {provider.models.map((item) => (
                  <option key={item.model} value={item.model}>
                    {modelOptionText(item)}
                  </option>
                ))}
                <option value={CUSTOM_MODEL_VALUE}>Custom model...</option>
              </SelectInput>
            </FormField>
            {modelChoice === CUSTOM_MODEL_VALUE && (
              <TextInput
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={provider.default_model || "model-name"}
                aria-label="Custom model"
                mono
                className="mt-2"
              />
            )}
            {selectedModel && capabilityChips(selectedModel).length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {capabilityChips(selectedModel).map((cap) => (
                  <span
                    key={cap}
                    className="inline-flex items-center rounded border border-runtime-line-soft/60 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-muted"
                  >
                    {cap}
                  </span>
                ))}
              </div>
            )}
          </div>
          <FormField
            label="API base URL"
            className="md:col-span-2"
            description={
              knownBase
                ? `LiteLLM's ${
                    provider.openai_compatible ? "OpenAI-compatible " : ""
                  }endpoint for ${provider.label}.`
                : "LiteLLM has no default endpoint for this provider — enter the OpenAI-compatible base URL."
            }
          >
            {baseLocked ? (
              <div className="flex items-center gap-2">
                <TextInput value={baseUrl} readOnly mono className="flex-1" />
                <ToolbarButton type="button" size="xs" onClick={() => setEditBase(true)}>
                  Change
                </ToolbarButton>
              </div>
            ) : (
              <TextInput
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.example.com/v1"
                mono
              />
            )}
          </FormField>
          <FormField
            label="API key"
            description={
              provider.key_env
                ? `Stored on the control plane and redacted. LiteLLM reads it as ${provider.key_env}.`
                : "Stored on the control plane and redacted when read back."
            }
            className="md:col-span-2"
          >
            <TextInput
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={provider.key_env ? `${provider.label} API key` : "API key"}
              required
              mono
            />
          </FormField>
        </div>

        <div className="mt-4">
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="text-xs font-medium text-ink-dim transition-colors hover:text-ink"
            aria-expanded={showAdvanced}
          >
            {showAdvanced ? "▾" : "▸"} Model options{selectedModel ? ` for ${selectedModel.model}` : ""}
          </button>
          {showAdvanced && (
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              {showTemperature && (
                <>
                  <FormField label="Temperature">
                    <SelectInput
                      value={temperatureMode}
                      onChange={(e) => setTemperatureMode(e.target.value as TemperatureMode)}
                    >
                      <option value="omit">do not send</option>
                      <option value="default">send 0.0</option>
                      <option value="custom">custom</option>
                    </SelectInput>
                  </FormField>
                  <FormField label="Value">
                    <TextInput
                      type="number"
                      min="0"
                      max="2"
                      step="0.1"
                      value={temperature}
                      onChange={(e) => setTemperature(e.target.value)}
                      disabled={temperatureMode !== "custom"}
                      mono
                    />
                  </FormField>
                </>
              )}
              {showMaxTokens && (
                <FormField label="Max output tokens">
                  <TextInput
                    type="number"
                    min="1"
                    step="1"
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(e.target.value)}
                    placeholder="model default"
                    mono
                  />
                </FormField>
              )}
              {showTopP && (
                <FormField label="Top-p">
                  <TextInput
                    type="number"
                    min="0"
                    max="1"
                    step="0.05"
                    value={topP}
                    onChange={(e) => setTopP(e.target.value)}
                    placeholder="model default"
                    mono
                  />
                </FormField>
              )}
              {showReasoning && (
                <FormField label="Reasoning effort">
                  <SelectInput
                    value={reasoningEffort}
                    onChange={(e) => setReasoningEffort(e.target.value)}
                  >
                    <option value="">model default</option>
                    <option value="minimal">minimal</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </SelectInput>
                </FormField>
              )}
              {showThinking && (
                <FormField label="Extended thinking">
                  <SelectInput
                    value={thinking}
                    onChange={(e) =>
                      setThinking(e.target.value as "default" | "enabled" | "disabled")
                    }
                  >
                    <option value="default">model default</option>
                    <option value="enabled">enabled</option>
                    <option value="disabled">disabled</option>
                  </SelectInput>
                </FormField>
              )}
              <div className="md:col-span-2">
                <div className="mb-1 flex items-center justify-between gap-3">
                  <span className="block text-xs font-medium text-ink-dim">
                    Extra request body (JSON)
                  </span>
                  {showKimiThinkingShortcut && (
                    <ToolbarButton
                      type="button"
                      size="xs"
                      onClick={() => setExtraBodyText(KIMI_THINKING_DISABLED)}
                    >
                      Kimi thinking off
                    </ToolbarButton>
                  )}
                </div>
                <TextArea
                  value={extraBodyText}
                  onChange={(e) => setExtraBodyText(e.target.value)}
                  placeholder='Optional. e.g. {"thinking":{"type":"disabled"}}'
                  rows={3}
                  aria-label="Extra request body JSON"
                  mono
                />
                <span className="mt-1 block text-xs leading-relaxed text-ink-muted">
                  Escape hatch for anything the controls above don't cover. Merged on top; most models need nothing here.
                </span>
              </div>
            </div>
          )}
        </div>
        {visibleValidationError && (
          <div className="mt-3">
            <InlineAlert tone="amber">{visibleValidationError}</InlineAlert>
          </div>
        )}
        {err && (
          <div className="mt-3">
            <InlineAlert tone="red">{err}</InlineAlert>
          </div>
        )}
        <div className="mt-3 flex justify-end">
          <ToolbarButton
            type="submit"
            variant="primary"
            size="md"
            disabled={busy || Boolean(validationError)}
          >
            {busy ? "Saving..." : "Save key"}
          </ToolbarButton>
        </div>
    </form>
  );
}

function ReadOnlyFact({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <SummaryMetric
      label={label}
      value={<span title={value}>{value}</span>}
      size="compact"
      mono={mono}
    />
  );
}

function llmKeyHealth(creds: LLMCreds[] | null, defaultCred: LLMCreds | null) {
  if (creds === null) {
    return { label: "checking", tone: "neutral" as const, detail: "loading saved credentials" };
  }
  if (defaultCred) {
    return { label: "ready", tone: "emerald" as const, detail: "default credential available" };
  }
  if (creds.length > 0) {
    return { label: "attention", tone: "amber" as const, detail: "add or rename a default key" };
  }
  return { label: "missing", tone: "amber" as const, detail: "add a provider key" };
}

function credentialNextAction(
  creds: LLMCreds[] | null,
  defaultCred: LLMCreds | null,
) {
  if (creds === null) {
    return {
      label: "Load saved credentials",
      detail: "Checking current provider keys and default fallback state.",
      href: "/llm-keys/saved",
      action: "Saved keys",
      tone: "secondary" as const,
    };
  }
  return credentialNextActionFromState(creds.length, defaultCred);
}

function credentialNextActionFromState(savedCount: number, defaultCred: LLMCreds | null) {
  if (!defaultCred && savedCount === 0) {
    return {
      label: "Add a default provider key",
      detail: "A credential named default gives chat and LLM-capable agents a fallback key.",
      href: "/llm-keys/add",
      action: "Add key",
      tone: "primary" as const,
    };
  }
  if (!defaultCred) {
    return {
      label: "Create or rename the default fallback",
      detail: "Saved keys exist, but chat has no default credential to fall back to.",
      href: "/llm-keys/add",
      action: "Add default",
      tone: "primary" as const,
    };
  }
  return {
    label: "Review saved provider coverage",
    detail: `${providerLabel(defaultCred.base_url)} is the current default for ${defaultCred.model}.`,
    href: "/llm-keys/saved",
    action: "Review keys",
    tone: "secondary" as const,
  };
}

function providerLabel(baseUrl: string): string {
  try {
    const host = new URL(baseUrl).hostname.replace(/^api\./, "");
    if (host.includes("openai.com")) return "OpenAI";
    if (host.includes("anthropic.com")) return "Anthropic";
    if (host.includes("moonshot") || host.includes("kimi")) return "Kimi";
    if (host.includes("openrouter.ai")) return "OpenRouter";
    if (host.includes("localhost") || host.includes("127.0.0.1")) return "Local";
    return host;
  } catch {
    return baseUrl || "custom";
  }
}

function formatCredentialDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "-";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function credentialFormError(
  name: string,
  baseUrl: string,
  model: string,
  apiKey: string,
  temperatureMode: TemperatureMode,
  temperature: string,
  extraBodyText: string,
): string | null {
  if (!/^[A-Za-z0-9_.-]{1,64}$/.test(name.trim())) {
    return "Credential name must use only letters, numbers, dots, dashes, or underscores.";
  }
  if (!model.trim()) return "Model is required.";
  try {
    const parsed = new URL(normalizeBaseUrl(baseUrl));
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "Base URL must start with http:// or https://.";
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      return "Base URL must not include credentials, query, or fragment.";
    }
  } catch {
    return "Base URL must be a valid http(s) URL.";
  }
  if (temperatureMode === "custom") {
    const value = Number(temperature);
    if (!Number.isFinite(value) || value < 0 || value > 2) {
      return "Temperature must be a number between 0 and 2.";
    }
  }
  try {
    parseExtraBody(extraBodyText);
  } catch (ex) {
    return ex instanceof Error ? ex.message : "Extra request body must be valid JSON.";
  }
  if (!apiKey.trim()) return "API key is required.";
  return null;
}

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  return trimmed.replace(/\/(chat\/completions|completions|responses)$/, "");
}

function temperatureLabel(mode: string, temperature: number | null): string {
  if (mode === "omit") return "temperature omitted";
  if (mode === "custom") return `temperature ${temperature ?? ""}`.trim();
  return "temperature 0.0";
}

function parseExtraBody(value: string): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed) as unknown;
  if (!isPlainObject(parsed)) {
    throw new Error("Extra request body must be a JSON object.");
  }
  return parsed;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExtraBody(value: Record<string, unknown> | null | undefined): boolean {
  return Boolean(value && Object.keys(value).length > 0);
}

function isKimiProvider(provider: LLMModelCatalogProvider, model: string): boolean {
  const bits = [
    provider.id,
    provider.provider,
    provider.label,
    provider.base_url,
    model,
  ].map((value) => String(value || "").toLowerCase());
  return bits.some((value) => value.includes("kimi") || value.includes("moonshot"));
}

function defaultModelForProvider(provider: LLMModelCatalogProvider): string {
  return provider.default_model || provider.models[0]?.model || "";
}

// Capability labels for a model, straight from litellm's support flags.
function capabilityChips(item: LLMModelCatalogModel): string[] {
  const chips: string[] = [];
  if (item.supports_vision) chips.push("Vision");
  if (item.supports_function_calling) chips.push("Tools");
  if (item.supports_reasoning) chips.push("Reasoning");
  if (item.supports_web_search) chips.push("Web search");
  return chips;
}

// Validate the numeric model-option knobs (only when they're shown + filled).
function modelOptionsError(
  showMaxTokens: boolean,
  maxTokens: string,
  showTopP: boolean,
  topP: string,
): string | null {
  if (showMaxTokens && maxTokens.trim()) {
    const n = Number(maxTokens);
    if (!Number.isInteger(n) || n <= 0) {
      return "Max output tokens must be a positive whole number.";
    }
  }
  if (showTopP && topP.trim()) {
    const n = Number(topP);
    if (!Number.isFinite(n) || n < 0 || n > 1) {
      return "Top-p must be a number between 0 and 1.";
    }
  }
  return null;
}

function modelSelectValue(
  model: string,
  provider: LLMModelCatalogProvider,
): string {
  return provider.models.some((item) => item.model === model)
    ? model
    : CUSTOM_MODEL_VALUE;
}

function modelOptionText(item: LLMModelCatalogModel): string {
  const label = modelOptionLabel(item);
  return label ? `${item.model} (${label})` : item.model;
}

function modelOptionLabel(item: LLMModelCatalogModel): string {
  const bits = [
    item.provider,
    item.input_cost_per_million_tokens !== null
      ? `$${formatCost(item.input_cost_per_million_tokens)}/M in`
      : null,
    item.output_cost_per_million_tokens !== null
      ? `$${formatCost(item.output_cost_per_million_tokens)}/M out`
      : null,
  ].filter(Boolean);
  return bits.join(" · ");
}

function formatCost(value: number): string {
  return value >= 10 ? value.toFixed(0) : value.toFixed(2);
}
