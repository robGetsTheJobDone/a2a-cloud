import { useEffect, useMemo, useState } from "react";
import {
  listLlmModelCatalog,
  updateOnboardingState,
  upsertLlmCreds,
  type LLMModelCatalog,
  type LLMModelCatalogModel,
  type LLMModelCatalogProvider,
  type OnboardingState,
} from "../api";
import {
  Dialog,
  InlineAlert,
  SelectInput,
  TextArea,
  TextInput,
  ToolbarButton,
} from "./DashboardChrome";
import { DashboardSurfacePosture } from "./SurfacePosture";
import { GuidedTour, type TourRoute } from "./GuidedTour";
import { TOUR_STEPS } from "./onboarding/tourSteps";

type DashboardRoute = TourRoute;

type Props = {
  state: OnboardingState;
  onStateChange: (state: OnboardingState) => void;
  onComplete: (state: OnboardingState) => void;
  onNavigate: (route: DashboardRoute) => void;
};

/** Where "Skip setup" drops a user who has not added a key yet: the shortest
 *  real path to a deployed agent, rather than an empty dashboard. */
const SKIP_DESTINATION: DashboardRoute = "studio";

/**
 * What an ordinary close gesture (Escape, backdrop, the X) does to the setup
 * dialog. Deliberately NOT the same thing as the labelled "Skip setup"
 * button: a stray Escape must not write `dismissed` to the account, must not
 * navigate, and must not throw away a key the user is halfway through pasting.
 *
 *  - "keep-open": there is unsaved key text, so the gesture is inert (Dialog
 *    only binds Escape/backdrop/X when it is handed an onClose).
 *  - "hide": nothing to lose, so hide the dialog for this session. Onboarding
 *    is untouched server-side and the dialog is back on the next load.
 */
export type DialogCloseGesture = "keep-open" | "hide";

export function closeGestureForDraft(draftApiKey: string): DialogCloseGesture {
  return draftApiKey.trim() ? "keep-open" : "hide";
}

/**
 * True when the server actually recorded the skip. A control plane older than
 * the `dismissed` field ignores it and still answers 200, and acting on that
 * would navigate to Studio with the modal still covering it.
 */
export function skipWasRecorded(state: OnboardingState): boolean {
  return state.dismissed === true;
}

type TemperatureMode = "omit" | "default" | "custom";

type ProviderPreset = {
  id: string;
  label: string;
  baseUrl: string;
  model: string;
  keyPlaceholder: string;
  extraBody?: Record<string, unknown>;
  models?: LLMModelCatalogModel[];
};

const CUSTOM_MODEL_VALUE = "__custom__";

const KIMI_THINKING_DISABLED = JSON.stringify(
  { thinking: { type: "disabled" } },
  null,
  2,
);

const PROVIDERS: ProviderPreset[] = [
  {
    id: "openai",
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-5",
    keyPlaceholder: "sk-...",
  },
  {
    id: "anthropic",
    label: "Anthropic",
    baseUrl: "https://api.anthropic.com/v1",
    model: "claude-sonnet-4-5",
    keyPlaceholder: "sk-ant-...",
  },
  {
    id: "google",
    label: "Google Gemini",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    model: "gemini-2.5-pro",
    keyPlaceholder: "AIza...",
  },
  {
    id: "kimi",
    label: "Kimi / Moonshot",
    baseUrl: "https://api.moonshot.ai/v1",
    model: "kimi-k2.6",
    keyPlaceholder: "sk-...",
    extraBody: { thinking: { type: "disabled" } },
  },
  {
    id: "litellm",
    label: "LiteLLM Gateway",
    baseUrl: "http://localhost:4000/v1",
    model: "gpt-5",
    keyPlaceholder: "sk-your-litellm-key",
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    model: "openrouter/auto",
    keyPlaceholder: "sk-or-...",
  },
  {
    id: "groq",
    label: "Groq",
    baseUrl: "https://api.groq.com/openai/v1",
    model: "openai/gpt-oss-120b",
    keyPlaceholder: "gsk_...",
  },
  {
    id: "mistral",
    label: "Mistral",
    baseUrl: "https://api.mistral.ai/v1",
    model: "mistral-large-latest",
    keyPlaceholder: "...",
  },
  {
    id: "cohere",
    label: "Cohere",
    baseUrl: "https://api.cohere.com/compatibility/v1",
    model: "command-a-03-2025",
    keyPlaceholder: "...",
  },
  {
    id: "xai",
    label: "xAI",
    baseUrl: "https://api.x.ai/v1",
    model: "grok-4",
    keyPlaceholder: "xai-...",
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
    keyPlaceholder: "sk-...",
  },
  {
    id: "ollama",
    label: "Local Ollama",
    baseUrl: "http://localhost:11434/v1",
    model: "qwen2.5-coder:14b",
    keyPlaceholder: "ollama",
  },
  {
    id: "custom",
    label: "Custom / LiteLLM",
    baseUrl: "https://api.example.com/v1",
    model: "provider/model",
    keyPlaceholder: "provider key",
  },
];

const fieldsetClass =
  "rounded-xl border border-runtime-line-soft/60 bg-runtime-panel/40 p-3.5";
const legendClass =
  "px-1 text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint";
const fieldLabelClass = "mb-1 block text-[11px] uppercase text-ink-muted";

export function OnboardingWizard({
  state,
  onStateChange,
  onComplete,
  onNavigate,
}: Props) {
  const [step, setStep] = useState<"llm_key" | "tour">(
    state.current_step === "tour" || state.llm_key_configured ? "tour" : "llm_key",
  );
  const [tourIndex, setTourIndex] = useState(
    Math.min(Math.max(state.tour_step_index || 0, 0), TOUR_STEPS.length - 1),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Session-only hide from a close gesture. Never persisted — see
  // closeGestureForDraft.
  const [hidden, setHidden] = useState(false);
  const [catalog, setCatalog] = useState<LLMModelCatalog | null>(null);
  const providers = useMemo(
    () => catalog?.providers?.length
      ? catalog.providers.map(catalogProviderToPreset)
      : PROVIDERS,
    [catalog],
  );
  const [providerId, setProviderId] = useState(PROVIDERS[0].id);
  const provider = useMemo(
    () => providers.find((item) => item.id === providerId) || providers[0],
    [providerId, providers],
  );
  const [name, setName] = useState("default");
  const [baseUrl, setBaseUrl] = useState(provider.baseUrl);
  const [model, setModel] = useState(provider.model);
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [temperatureMode, setTemperatureMode] = useState<TemperatureMode>("omit");
  const [temperature, setTemperature] = useState("1");
  const [extraBodyText, setExtraBodyText] = useState("");
  const modelChoice = modelSelectValue(model, provider);
  const showKimiThinkingShortcut = isKimiProvider(provider, model);

  useEffect(() => {
    listLlmModelCatalog()
      .then(setCatalog)
      .catch(() => setCatalog(null));
  }, []);

  useEffect(() => {
    const next = providers.find((item) => item.id === providerId) || providers[0];
    setBaseUrl(next.baseUrl);
    setModel(defaultModelForProvider(next));
    setExtraBodyText(next.extraBody ? JSON.stringify(next.extraBody, null, 2) : "");
  }, [providerId, providers]);

  useEffect(() => {
    if (state.current_step === "tour" || state.llm_key_configured) {
      setStep("tour");
    }
  }, [state.current_step, state.llm_key_configured]);

  const activeTour = TOUR_STEPS[tourIndex];
  const validationError = credentialFormError(
    name,
    baseUrl,
    model,
    apiKey,
    temperatureMode,
    temperature,
    extraBodyText,
  );

  useEffect(() => {
    if (step !== "tour") return;
    let cancelled = false;
    updateOnboardingState({
      current_step: "tour",
      last_seen_step: activeTour.target,
      tour_step_index: tourIndex,
      tour_step_total: TOUR_STEPS.length,
    })
      .then((next) => {
        if (!cancelled) onStateChange(next);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeTour.target, onStateChange, step, tourIndex]);

  async function saveKey(event: React.FormEvent) {
    event.preventDefault();
    if (validationError || busy) {
      if (validationError) setErr(validationError);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await upsertLlmCreds({
        name: name.trim(),
        base_url: normalizeBaseUrl(baseUrl),
        api_key: apiKey.trim(),
        model: model.trim(),
        temperature_mode: temperatureMode,
        temperature: temperatureMode === "custom" ? Number(temperature) : null,
        extra_body: parseExtraBody(extraBodyText),
      });
      setApiKey("");
      const next = await updateOnboardingState({
        current_step: "tour",
        llm_key_step_completed: true,
      });
      onStateChange(next);
      setStep("tour");
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function continueWithExistingKey() {
    if (!state.llm_key_configured || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const next = await updateOnboardingState({
        current_step: "tour",
        llm_key_step_completed: true,
      });
      onStateChange(next);
      setStep("tour");
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  // BYOK is deferrable, not optional. Dismissing records a `dismissed` flag the
  // server accepts without a key (unlike `completed`), so the wall comes down
  // and the key is asked for again at the moment chat or an LLM-backed agent
  // actually needs it. Reached only from the labelled button — never from a
  // stray Escape or backdrop click.
  async function skipForNow() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const next = await updateOnboardingState({ dismissed: true });
      if (!skipWasRecorded(next)) {
        setErr(
          "This server did not record the skip. Add a key to continue, or try again shortly.",
        );
        return;
      }
      onComplete(next);
      onNavigate(SKIP_DESTINATION);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      const next = await updateOnboardingState({ completed: true });
      onComplete(next);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  function nextTour() {
    if (tourIndex >= TOUR_STEPS.length - 1) {
      void finish();
      return;
    }
    setTourIndex((current) => Math.min(current + 1, TOUR_STEPS.length - 1));
  }

  function previousTour() {
    setTourIndex((current) => Math.max(current - 1, 0));
  }

  if (step === "tour") {
    return (
      <GuidedTour
        activeTour={activeTour}
        busy={busy}
        error={err}
        index={tourIndex}
        total={TOUR_STEPS.length}
        onBack={previousTour}
        onComplete={finish}
        onNavigate={onNavigate}
        onNext={nextTour}
      />
    );
  }

  return (
    <Dialog
      open={!state.completed && !state.dismissed && !hidden}
      title="Set up your dashboard"
      description="Add a model key now, or skip and add it when chat or an LLM-capable agent first needs one."
      size="lg"
      onClose={
        busy || closeGestureForDraft(apiKey) === "keep-open"
          ? undefined
          : () => setHidden(true)
      }
    >
      <form onSubmit={saveKey} className="space-y-3.5">
        <DashboardSurfacePosture
          className="sticky top-0 z-10"
          eyebrow="Optional now"
          title="LLM key"
          status={{ label: "Active", tone: "authority" }}
          metrics={[{ label: "Skip goes to", value: "Studio", tone: "neutral" }]}
        />
        {state.llm_key_configured && (
          <InlineAlert tone="neutral">
            A saved LLM key is already configured for this account.
          </InlineAlert>
        )}

        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Provider</legend>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="block">
              <span className={fieldLabelClass}>Provider</span>
              <SelectInput
                value={providerId}
                onChange={(event) => setProviderId(event.target.value)}
              >
                {providers.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </SelectInput>
            </label>
            <label className="block">
              <span className={fieldLabelClass}>Name</span>
              <TextInput
                value={name}
                onChange={(event) => setName(event.target.value)}
                mono
              />
            </label>
            <label className="block md:col-span-2">
              <span className={fieldLabelClass}>API base URL</span>
              <TextInput
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                mono
              />
            </label>
          </div>
        </fieldset>

        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Model</legend>
          <label className="block">
            <span className={fieldLabelClass}>Model</span>
            <SelectInput
              value={modelChoice}
              onChange={(event) => {
                const next = event.target.value;
                setModel(next === CUSTOM_MODEL_VALUE ? "" : next);
              }}
              className="font-mono"
            >
              {(provider.models || []).map((item) => (
                <option key={item.model} value={item.model}>
                  {modelOptionText(item)}
                </option>
              ))}
              <option value={CUSTOM_MODEL_VALUE}>Custom model...</option>
            </SelectInput>
            {modelChoice === CUSTOM_MODEL_VALUE && (
              <TextInput
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder={provider.model || "provider/model"}
                mono
                className="mt-2"
              />
            )}
            {provider.models && provider.models.length > 0 && (
              <span className="mt-1 block text-[11px] text-ink-muted">
                {provider.models.length} LiteLLM-compatible models available.
              </span>
            )}
          </label>
        </fieldset>

        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Auth &amp; options</legend>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="block">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-[11px] uppercase text-ink-muted">
                  API key
                </span>
                <ToolbarButton
                  type="button"
                  onClick={() => setShowApiKey((current) => !current)}
                  size="xs"
                  variant="ghost"
                >
                  {showApiKey ? "Hide" : "Show"}
                </ToolbarButton>
              </div>
              <TextInput
                type={showApiKey ? "text" : "password"}
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder={provider.keyPlaceholder}
                mono
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className={fieldLabelClass}>Temperature</span>
                <SelectInput
                  value={temperatureMode}
                  onChange={(event) =>
                    setTemperatureMode(event.target.value as TemperatureMode)
                  }
                >
                  <option value="omit">do not send</option>
                  <option value="default">send 0.0</option>
                  <option value="custom">custom</option>
                </SelectInput>
              </label>
              <label className="block">
                <span className={fieldLabelClass}>Value</span>
                <TextInput
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                  value={temperature}
                  disabled={temperatureMode !== "custom"}
                  onChange={(event) => setTemperature(event.target.value)}
                  mono
                />
              </label>
            </div>
            <label className="block md:col-span-2">
              <div className="mb-1 flex items-center justify-between gap-3">
                <span className="block text-[11px] uppercase text-ink-muted">
                  Provider options JSON
                </span>
                {showKimiThinkingShortcut && (
                  <ToolbarButton
                    type="button"
                    onClick={() => setExtraBodyText(KIMI_THINKING_DISABLED)}
                    size="xs"
                  >
                    Kimi thinking off
                  </ToolbarButton>
                )}
              </div>
              <TextArea
                value={extraBodyText}
                onChange={(event) => setExtraBodyText(event.target.value)}
                rows={3}
                mono
                compact
              />
              <span className="mt-1 block text-[11px] text-ink-muted">
                Sent as provider-specific request fields. LiteLLM tracking metadata is attached separately.
              </span>
            </label>
          </div>
        </fieldset>

        {err && <InlineAlert tone="red">{err}</InlineAlert>}

        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* Says "skip setup", not "skip for now": this retires the setup
              dialog and the walkthrough for good. The key itself is still
              asked for later, at the point chat or an agent needs it. */}
          <ToolbarButton type="button" onClick={skipForNow} disabled={busy}>
            Skip setup — build an agent
          </ToolbarButton>
          <div className="flex flex-wrap gap-2">
            {state.llm_key_configured && (
              <ToolbarButton onClick={continueWithExistingKey} disabled={busy}>
                Use existing key
              </ToolbarButton>
            )}
            <ToolbarButton
              type="submit"
              variant="primary"
              disabled={busy || Boolean(validationError)}
            >
              {busy ? "Saving..." : "Save key and continue"}
            </ToolbarButton>
          </div>
        </div>
      </form>
    </Dialog>
  );
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

function catalogProviderToPreset(provider: LLMModelCatalogProvider): ProviderPreset {
  return {
    id: provider.id,
    label: provider.label,
    baseUrl: provider.base_url,
    model: provider.default_model,
    keyPlaceholder: provider.key_env ? `${provider.label} API key` : "API key",
    extraBody: provider.extra_body,
    models: provider.models,
  };
}

function defaultModelForProvider(provider: ProviderPreset): string {
  return provider.model || provider.models?.[0]?.model || "";
}

function modelSelectValue(model: string, provider: ProviderPreset): string {
  return provider.models?.some((item) => item.model === model)
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

function parseExtraBody(value: string): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed) as unknown;
  if (!isPlainObject(parsed)) {
    throw new Error("Extra request body must be a JSON object.");
  }
  return parsed;
}

function isKimiProvider(provider: ProviderPreset, model: string): boolean {
  const bits = [
    provider.id,
    provider.label,
    provider.baseUrl,
    model,
  ].map((value) => String(value || "").toLowerCase());
  return bits.some((value) => value.includes("kimi") || value.includes("moonshot"));
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
