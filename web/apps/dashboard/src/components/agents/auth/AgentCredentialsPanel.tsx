import { useCallback, useEffect, useState } from "react";
import {
  connectAgentAuth,
  deleteAgentAuth,
  getAgentAuth,
  type AgentAuthConnectInput,
  type AgentAuthStatus,
  type MyAgentListing,
} from "../../../api";
import {
  FormField,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  TextArea,
  TextInput,
  ToolbarButton,
} from "../../DashboardChrome";
import { fmtDate } from "../agentTypes";

export function AgentAuthPanel({ agent }: { agent: MyAgentListing }) {
  const [status, setStatus] = useState<AgentAuthStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [schemeName, setSchemeName] = useState("");
  const [schemeType, setSchemeType] = useState<AgentAuthConnectInput["scheme_type"]>("api_key");
  const [value, setValue] = useState("");
  const [paramName, setParamName] = useState("X-API-Key");
  const [location, setLocation] = useState<"header" | "query">("header");
  const [tokenUrl, setTokenUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [expiresIn, setExpiresIn] = useState("");
  const [certPem, setCertPem] = useState("");
  const [keyPem, setKeyPem] = useState("");
  const [caPem, setCaPem] = useState("");

  const load = useCallback(async () => {
    try {
      setStatus(await getAgentAuth(agent.name));
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }, [agent.name]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const first = status?.requirements[0];
    if (!first) return;
    setSchemeName(first.scheme_name);
    if (["api_key", "http", "oauth2", "oidc", "mtls"].includes(first.scheme_type)) {
      setSchemeType(first.scheme_type as AgentAuthConnectInput["scheme_type"]);
    }
    if (first.name) setParamName(first.name);
    if (first.location === "query" || first.location === "header") {
      setLocation(first.location);
    }
    if (first.token_url) setTokenUrl(first.token_url);
  }, [status?.agent_name]);

  async function connect() {
    setBusy(true);
    setErr(null);
    try {
      const input: AgentAuthConnectInput = {
        scheme_type: schemeType,
        scheme_name: schemeName || undefined,
      };
      if (schemeType === "api_key") {
        input.value = value;
        input.name = paramName;
        input.location = location;
      } else if (schemeType === "http") {
        input.token = value;
        input.scheme = "Bearer";
      } else if (schemeType === "oauth2" || schemeType === "oidc") {
        input.access_token = value;
        input.refresh_token = refreshToken || undefined;
        input.token_url = tokenUrl || undefined;
        input.client_id = clientId || undefined;
        input.client_secret = clientSecret || undefined;
        input.expires_in = expiresIn ? Number(expiresIn) : undefined;
      } else if (schemeType === "mtls") {
        input.cert_pem = certPem;
        input.key_pem = keyPem;
        input.ca_pem = caPem || undefined;
      }
      await connectAgentAuth(agent.name, input);
      setValue("");
      setClientSecret("");
      await load();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function remove(connectionId: number) {
    setBusy(true);
    setErr(null);
    try {
      await deleteAgentAuth(agent.name, connectionId);
      await load();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] uppercase text-ink-faint">imported auth</div>
          <div className="mt-1 text-sm text-ink-soft">
            {status ? status.status : "loading..."}
          </div>
        </div>
        <ToolbarButton onClick={load} disabled={busy} size="sm">
          refresh
        </ToolbarButton>
      </div>

      {status && status.requirements.length > 0 && (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {status.requirements.map((req) => (
            <SurfacePanel as="article" key={req.scheme_name} className="p-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-ink">{req.scheme_name}</span>
                <span className="text-ink-muted">{req.scheme_type}</span>
              </div>
              <div className="mt-1 text-ink-muted">{authRequirementSummary(req)}</div>
            </SurfacePanel>
          ))}
        </div>
      )}

      {status && status.connections.length > 0 && (
        <div className="mt-3 space-y-2">
          {status.connections.map((connection) => (
            <SurfacePanel
              as="article"
              key={connection.id}
              className="grid gap-2 p-3 text-xs sm:grid-cols-[1fr_auto]"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-ink">{connection.scheme_name}</span>
                  <span className="text-ink-muted">{connection.scheme_type}</span>
                  <span className={connectionStatusColor(connection.status)}>
                    {connection.status}
                  </span>
                </div>
                <div className="mt-1 text-ink-faint">
                  {connection.expires_at ? `expires ${fmtDate(connection.expires_at)}` : "no expiry"}
                </div>
              </div>
              <ToolbarButton
                onClick={() => remove(connection.id)}
                disabled={busy}
                variant="danger"
                size="sm"
              >
                remove
              </ToolbarButton>
            </SurfacePanel>
          ))}
        </div>
      )}

      <SurfacePanel as="div" className="mt-3 grid gap-3 p-3 text-xs lg:grid-cols-2">
        <FormField label="Scheme">
          <TextInput
            value={schemeName}
            onChange={(e) => setSchemeName(e.target.value)}
            mono
            compact
          />
        </FormField>
        <FormField label="Type">
          <SelectInput
            value={schemeType}
            onChange={(e) => setSchemeType(e.target.value as AgentAuthConnectInput["scheme_type"])}
            compact
          >
            <option value="api_key">api key</option>
            <option value="http">bearer/http</option>
            <option value="oauth2">oauth2</option>
            <option value="oidc">oidc</option>
            <option value="mtls">mtls</option>
          </SelectInput>
        </FormField>

        {schemeType === "api_key" && (
          <>
            <FormField label="Name">
              <TextInput
                value={paramName}
                onChange={(e) => setParamName(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Location">
              <SelectInput
                value={location}
                onChange={(e) => setLocation(e.target.value as "header" | "query")}
                compact
              >
                <option value="header">header</option>
                <option value="query">query</option>
              </SelectInput>
            </FormField>
          </>
        )}

        {(schemeType === "api_key" || schemeType === "http") && (
          <FormField label="Credential" className="lg:col-span-2">
            <TextInput
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              mono
              compact
            />
          </FormField>
        )}

        {(schemeType === "oauth2" || schemeType === "oidc") && (
          <>
            <FormField label="Access token" className="lg:col-span-2">
              <TextInput
                type="password"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Token URL">
              <TextInput
                value={tokenUrl}
                onChange={(e) => setTokenUrl(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Expires in">
              <TextInput
                value={expiresIn}
                onChange={(e) => setExpiresIn(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Client ID">
              <TextInput
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Client secret">
              <TextInput
                type="password"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                mono
                compact
              />
            </FormField>
            <FormField label="Refresh token" className="lg:col-span-2">
              <TextInput
                type="password"
                value={refreshToken}
                onChange={(e) => setRefreshToken(e.target.value)}
                mono
                compact
              />
            </FormField>
          </>
        )}

        {schemeType === "mtls" && (
          <>
            <PemBox label="cert pem" value={certPem} onChange={setCertPem} />
            <PemBox label="key pem" value={keyPem} onChange={setKeyPem} />
            <PemBox label="ca pem" value={caPem} onChange={setCaPem} />
          </>
        )}

        <div className="flex flex-col gap-2 lg:col-span-2 sm:flex-row sm:items-center sm:justify-end">
          {err && (
            <InlineAlert tone="red" role="alert" className="text-xs sm:mr-auto">
              {err}
            </InlineAlert>
          )}
          <ToolbarButton onClick={connect} disabled={busy} variant="success" size="md">
            {busy ? "saving..." : "save auth"}
          </ToolbarButton>
        </div>
      </SurfacePanel>
    </div>
  );
}

function PemBox({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <FormField label={label} className="lg:col-span-2">
      <TextArea value={value} onChange={(e) => onChange(e.target.value)} rows={4} mono compact />
    </FormField>
  );
}

function authRequirementSummary(req: AgentAuthStatus["requirements"][number]): string {
  if (req.scheme_type === "api_key") {
    return `${req.location || "header"} ${req.name || "api key"}`;
  }
  if (req.scheme_type === "http") return req.scheme || "HTTP auth";
  if (req.scheme_type === "oauth2") return req.token_url || "OAuth2 token";
  if (req.scheme_type === "oidc") return req.open_id_connect_url || "OpenID Connect";
  if (req.scheme_type === "mtls") return "client certificate";
  return req.supported ? "supported" : "unsupported";
}

function connectionStatusColor(status: string): string {
  if (status === "connected") return "text-signal-live";
  if (status === "failed" || status === "expired") return "text-signal-danger";
  return "text-signal-authority";
}
