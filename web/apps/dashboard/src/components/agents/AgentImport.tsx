import { useEffect, useState } from "react";
import {
  generateOpenApiAgent,
  importAgent,
  previewOpenApiAgent,
  type AgentImportAuthInput,
  type AgentOpenApiPreview,
} from "../../api";
import { agentDetailRouteReference } from "../myAgentRouteState";
import { OpenApiPreviewCard } from "../OpenApiPreviewCard";
import {
  FormField,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  TabPill,
  TextArea,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import { type AgentIndexAgent, splitOpenApiUrls } from "./agentTypes";

type ImportAuthMode = "none" | "bearer" | "api_key";
type ImportPanelMode = "openapi" | "a2a";

const PETSTORE_OPENAPI_URL = "https://petstore3.swagger.io/api/v3/openapi.json";

export function ImportAgentPage({
  agents,
  requestedAgentName,
  onCancel,
  onImported,
}: {
  agents: AgentIndexAgent[];
  requestedAgentName: string | null;
  onCancel: () => void;
  onImported: () => Promise<void>;
}) {
  const routeReference = requestedAgentName
    ? agentDetailRouteReference(requestedAgentName)
    : null;

  return (
    <div className="space-y-4">
      <SurfacePanel as="section" className="bg-runtime-bg p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="text-xs uppercase text-ink-muted">
              Agent intake
            </div>
            <h2 className="mt-1 text-xl font-semibold text-ink">
              Bring an A2A agent
            </h2>
            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-ink-muted">
              Import an existing A2A endpoint or generate a managed agent from an OpenAPI specification.
            </p>
            {requestedAgentName && routeReference && (
              <div className="mt-3 rounded-md border border-signal-authority/45 bg-signal-authority/12 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <StateBadge status="requested" tone="amber" size="xs" />
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-signal-authority">
                    Missing agent route
                  </span>
                </div>
                <div className="mt-2 break-all font-mono text-sm text-ink">
                  {routeReference}
                </div>
                <p className="mt-2 max-w-2xl text-xs leading-relaxed text-ink-dim">
                  The form is set to A2A URL import and the registry name is
                  prefilled. Add the agent endpoint to attach this route to your
                  fleet.
                </p>
              </div>
            )}
          </div>
          <ToolbarButton type="button" onClick={onCancel}>
            Back to agents
          </ToolbarButton>
        </div>
      </SurfacePanel>
      <ImportAgentPanel
        agents={agents}
        requestedAgentName={requestedAgentName}
        onCancel={onCancel}
        onImported={onImported}
      />
    </div>
  );
}

function ImportAgentPanel({
  agents,
  requestedAgentName,
  onCancel,
  onImported,
}: {
  agents: AgentIndexAgent[];
  requestedAgentName: string | null;
  onCancel: () => void;
  onImported: () => Promise<void>;
}) {
  const requestedAgentValue = requestedAgentName?.trim() || "";
  const [mode, setMode] = useState<ImportPanelMode>(() =>
    requestedAgentValue ? "a2a" : "openapi",
  );
  const [url, setUrl] = useState("");
  const [name, setName] = useState(requestedAgentValue);
  const [isPublic, setIsPublic] = useState(false);
  const [authMode, setAuthMode] = useState<ImportAuthMode>("none");
  const [bearer, setBearer] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyName, setApiKeyName] = useState("X-API-Key");
  const [apiKeyLocation, setApiKeyLocation] = useState<"header" | "query">("header");
  const [openApiUrl, setOpenApiUrl] = useState(PETSTORE_OPENAPI_URL);
  const [openApiName, setOpenApiName] = useState(
    requestedAgentValue || "petstore-auto",
  );
  const [openApiDescription, setOpenApiDescription] = useState("");
  const [openApiBaseUrl, setOpenApiBaseUrl] = useState("");
  const [openApiPublic, setOpenApiPublic] = useState(true);
  const [preview, setPreview] = useState<AgentOpenApiPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [lastRequestedAgentValue, setLastRequestedAgentValue] =
    useState(requestedAgentValue);

  useEffect(() => {
    if (!requestedAgentValue || requestedAgentValue === lastRequestedAgentValue) {
      return;
    }
    setName((current) =>
      !current.trim() || current === lastRequestedAgentValue
        ? requestedAgentValue
        : current,
    );
    setOpenApiName((current) =>
      !current.trim() ||
      current === "petstore-auto" ||
      current === lastRequestedAgentValue
        ? requestedAgentValue
        : current,
    );
    setMode((current) =>
      current === "openapi" && !url.trim() ? "a2a" : current,
    );
    setLastRequestedAgentValue(requestedAgentValue);
  }, [lastRequestedAgentValue, requestedAgentValue, url]);

  const canImportA2A =
    url.trim().length > 0 &&
    (authMode === "none" ||
      (authMode === "bearer" && bearer.trim().length > 0) ||
      (authMode === "api_key" &&
        apiKey.trim().length > 0 &&
        apiKeyName.trim().length > 0));
  const openApiUrls = splitOpenApiUrls(openApiUrl);
  const canGenerateOpenApi = openApiUrls.length > 0;
  const canSubmit = mode === "openapi" ? canGenerateOpenApi : canImportA2A;

  const openApiPayload = (refreshExisting = false) => ({
    url: openApiUrls[0],
    urls: openApiUrls.length > 1 ? openApiUrls : undefined,
    name: openApiName.trim() || undefined,
    description: openApiDescription.trim() || undefined,
    public: openApiPublic,
    base_url: openApiBaseUrl.trim() || undefined,
    refresh_existing: refreshExisting || undefined,
  });

  async function fetchOpenApiPreview() {
    const next = await previewOpenApiAgent(openApiPayload());
    setPreview(next);
    return next;
  }

  async function loadPreview() {
    if (!canGenerateOpenApi || previewBusy) return;
    setPreviewBusy(true);
    setErr(null);
    try {
      await fetchOpenApiPreview();
    } catch (ex) {
      setPreview(null);
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function submit() {
    if (!canSubmit || busy) return;
    setBusy(true);
    setErr(null);
    try {
      if (mode === "openapi") {
        let nextPreview = preview;
        if (!nextPreview) {
          setPreviewBusy(true);
          try {
            nextPreview = await fetchOpenApiPreview();
          } finally {
            setPreviewBusy(false);
          }
        }
        const existingAgent = agents.find((agent) => agent.name === nextPreview.name);
        if (existingAgent) {
          const confirmed = window.confirm(
            `Refresh ${nextPreview.name}? This will regenerate and redeploy the existing agent from the current OpenAPI spec.`,
          );
          if (!confirmed) return;
        }
        await generateOpenApiAgent(openApiPayload(Boolean(existingAgent)));
        setPreview(null);
      } else {
        let auth: AgentImportAuthInput | undefined;
        if (authMode === "bearer") {
          auth = { type: "bearer", value: bearer.trim(), scheme: "Bearer" };
        } else if (authMode === "api_key") {
          auth = {
            type: "api_key",
            value: apiKey.trim(),
            location: apiKeyLocation,
            name: apiKeyName.trim(),
          };
        }
        await importAgent({
          url: url.trim(),
          name: name.trim() || undefined,
          public: isPublic,
          auth,
        });
        setUrl("");
        setName("");
        setBearer("");
        setApiKey("");
      }
      await onImported();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  const existingOpenApiAgent = preview
    ? agents.find((agent) => agent.name === preview.name) || null
    : null;
  const submitLabel =
    busy
      ? "working..."
      : mode === "openapi"
        ? existingOpenApiAgent
          ? "refresh"
          : "generate"
        : "import";

  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="mb-4 grid grid-cols-2 rounded-md border border-runtime-line-soft/60 bg-runtime-panel p-1 text-xs">
        {(["openapi", "a2a"] as ImportPanelMode[]).map((nextMode) => (
          <TabPill
            key={nextMode}
            type="button"
            onClick={() => setMode(nextMode)}
            disabled={busy}
            selected={mode === nextMode}
            role="tab"
          >
            {nextMode === "openapi" ? "OpenAPI" : "A2A URL"}
          </TabPill>
        ))}
      </div>

      {mode === "openapi" ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(280px,0.8fr)]">
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="OpenAPI URLs" className="sm:col-span-2">
              <div className="mt-1 flex gap-2">
                <TextArea
                  value={openApiUrl}
                  onChange={(e) => {
                    setOpenApiUrl(e.target.value);
                    setPreview(null);
                  }}
                  placeholder={PETSTORE_OPENAPI_URL}
                  disabled={busy}
                  rows={2}
                  mono
                  className="min-w-0 flex-1 resize-none"
                />
                <ToolbarButton
                  type="button"
                  onClick={() => {
                    setOpenApiUrl(PETSTORE_OPENAPI_URL);
                    setOpenApiName("petstore-auto");
                    setPreview(null);
                  }}
                  disabled={busy}
                  size="md"
                >
                  Petstore
                </ToolbarButton>
              </div>
            </FormField>
            <FormField label="Registry name">
              <TextInput
                value={openApiName}
                onChange={(e) => {
                  setOpenApiName(e.target.value);
                  setPreview(null);
                }}
                placeholder="auto"
                disabled={busy}
                mono
              />
            </FormField>
            <FormField label="Visibility">
              <SelectInput
                value={openApiPublic ? "public" : "private"}
                onChange={(e) => setOpenApiPublic(e.target.value === "public")}
                disabled={busy}
              >
                <option value="public">public</option>
                <option value="private">private</option>
              </SelectInput>
            </FormField>
            <FormField label="API base URL" className="sm:col-span-2">
              <TextInput
                value={openApiBaseUrl}
                onChange={(e) => {
                  setOpenApiBaseUrl(e.target.value);
                  setPreview(null);
                }}
                placeholder="from spec"
                disabled={busy}
                mono
              />
            </FormField>
            <FormField label="Description" className="sm:col-span-2">
              <TextArea
                value={openApiDescription}
                onChange={(e) => {
                  setOpenApiDescription(e.target.value);
                  setPreview(null);
                }}
                rows={3}
                disabled={busy}
                className="resize-none"
              />
            </FormField>
          </div>

          <OpenApiPreviewCard
            preview={preview}
            busy={previewBusy}
            onPreview={loadPreview}
            existingAgentName={existingOpenApiAgent?.name || null}
          />
        </div>
      ) : (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,0.8fr)]">
          <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Agent URL" className="sm:col-span-2">
            <TextInput
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://agent.example.com"
              disabled={busy}
              mono
            />
          </FormField>
          <FormField label="Registry name">
            <TextInput
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="auto"
              disabled={busy}
              mono
            />
          </FormField>
          <FormField label="Visibility">
            <SelectInput
              value={isPublic ? "public" : "private"}
              onChange={(e) => setIsPublic(e.target.value === "public")}
              disabled={busy}
            >
              <option value="private">private</option>
              <option value="public">public</option>
            </SelectInput>
          </FormField>
        </div>

        <div className="grid gap-3">
          <div>
            <div className="text-[10px] uppercase text-ink-faint">
              auth
            </div>
            <div className="mt-1 grid grid-cols-3 rounded-md border border-runtime-line-soft/60 bg-runtime-panel p-1 text-xs">
              {(["none", "bearer", "api_key"] as ImportAuthMode[]).map((nextAuthMode) => (
                <TabPill
                  key={nextAuthMode}
                  type="button"
                  onClick={() => setAuthMode(nextAuthMode)}
                  disabled={busy}
                  selected={authMode === nextAuthMode}
                  role="tab"
                >
                  {nextAuthMode === "api_key" ? "api key" : nextAuthMode}
                </TabPill>
              ))}
            </div>
          </div>

          {authMode === "bearer" && (
            <FormField label="Bearer token">
              <TextInput
                type="password"
                value={bearer}
                onChange={(e) => setBearer(e.target.value)}
                disabled={busy}
                mono
              />
            </FormField>
          )}

          {authMode === "api_key" && (
            <div className="grid gap-3 sm:grid-cols-[1fr_120px] lg:grid-cols-1 xl:grid-cols-[1fr_120px]">
              <FormField label="Key value" className="sm:col-span-2 lg:col-span-1 xl:col-span-2">
                <TextInput
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  disabled={busy}
                  mono
                />
              </FormField>
              <FormField label="Name">
                <TextInput
                  value={apiKeyName}
                  onChange={(e) => setApiKeyName(e.target.value)}
                  disabled={busy}
                  mono
                />
              </FormField>
              <FormField label="Location">
                <SelectInput
                  value={apiKeyLocation}
                  onChange={(e) => setApiKeyLocation(e.target.value as "header" | "query")}
                  disabled={busy}
                >
                  <option value="header">header</option>
                  <option value="query">query</option>
                </SelectInput>
              </FormField>
            </div>
          )}
        </div>
        </div>
      )}

      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
        {err && (
          <InlineAlert tone="red" role="alert" className="text-xs sm:mr-auto">
            {err}
          </InlineAlert>
        )}
        <ToolbarButton
          onClick={onCancel}
          disabled={busy}
          size="md"
        >
          cancel
        </ToolbarButton>
        <ToolbarButton
          onClick={submit}
          disabled={!canSubmit || busy}
          variant="success"
          size="md"
        >
          {submitLabel}
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}
