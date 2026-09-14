import { useCallback, useEffect, useState } from "react";
import {
  createAgentIntegrationLink,
  createAgentApiToken,
  integrationLinkUrlLabel,
  integrationLinkUrlSecrecyNote,
  listAgentIntegrationLinks,
  listAgentApiTokens,
  revokeAgentIntegrationLink,
  revokeAgentApiToken,
  type AgentApiToken,
  type AgentApiTokenCreated,
  type AgentIntegrationLink,
  type AgentIntegrationLinkCreated,
  type MyAgentListing,
} from "../../../api";
import {
  CopyValueField,
  InlineAlert,
  SurfacePanel,
  TextInput,
  ToolbarButton,
} from "../../DashboardChrome";
import { type AgentApiUrls, fmtDate } from "../agentTypes";

export function AgentApiAccessPanel({
  agent,
  urls,
}: {
  agent: MyAgentListing;
  urls: AgentApiUrls;
}) {
  const [tokens, setTokens] = useState<AgentApiToken[] | null>(null);
  const [newToken, setNewToken] = useState<AgentApiTokenCreated | null>(null);
  const [links, setLinks] = useState<AgentIntegrationLink[] | null>(null);
  const [newLink, setNewLink] = useState<AgentIntegrationLinkCreated | null>(null);
  const [label, setLabel] = useState(`${agent.name} API token`);
  const [urlToken, setUrlToken] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextTokens, nextLinks] = await Promise.all([
        listAgentApiTokens(agent.name),
        listAgentIntegrationLinks(agent.name),
      ]);
      setTokens(nextTokens);
      setLinks(nextLinks);
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }, [agent.name]);

  useEffect(() => {
    load();
  }, [load]);

  async function mintToken() {
    if (busy) return;
    setBusy("mint");
    setErr(null);
    setNewToken(null);
    try {
      const token = await createAgentApiToken(agent.name, label);
      setNewToken(token);
      setTokens((current) => [token, ...(current || [])]);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function mintIntegrationLink() {
    if (busy) return;
    setBusy("link");
    setErr(null);
    setNewLink(null);
    try {
      const link = await createAgentIntegrationLink(agent.name, { urlToken });
      setNewLink(link);
      setLinks((current) => [link, ...(current || [])]);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function revoke(token: AgentApiToken) {
    if (busy) return;
    setBusy(`revoke:${token.id}`);
    setErr(null);
    try {
      await revokeAgentApiToken(agent.name, token.id);
      setTokens((current) =>
        (current || []).map((item) =>
          item.id === token.id ? { ...item, enabled: false } : item,
        ),
      );
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function revokeLink(link: AgentIntegrationLink) {
    if (busy) return;
    setBusy(`link:${link.id}`);
    setErr(null);
    try {
      await revokeAgentIntegrationLink(agent.name, link.id);
      setLinks((current) =>
        (current || []).map((item) =>
          item.id === link.id ? { ...item, enabled: false } : item,
        ),
      );
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  const sampleCommand = newToken?.token
    ? sampleCurl({
        url: urls.sampleInvoke || urls.invokeBase,
        token: newToken.token,
      })
    : null;

  return (
    <div className="mt-4 border-t border-runtime-line-soft/70 pt-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-[10px] uppercase text-ink-muted">App API</div>
          <div className="mt-1 text-xs text-ink-muted">
            Use platform-minted bearer tokens to call this agent through its
            OpenAPI-compatible endpoint.
          </div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[minmax(220px,1fr)_auto]">
          <TextInput
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            disabled={Boolean(busy)}
            compact
          />
          <ToolbarButton
            onClick={mintToken}
            disabled={Boolean(busy)}
            variant="primary"
            size="md"
          >
            {busy === "mint" ? "Minting..." : "Mint token"}
          </ToolbarButton>
        </div>
      </div>

      {err && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {err}
        </InlineAlert>
      )}
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <CopyValueField label="OpenAPI" value={urls.openapi} />
        <CopyValueField label="Invoke base" value={urls.invokeBase} />
        {urls.sampleInvoke && (
          <CopyValueField label="Sample invoke" value={urls.sampleInvoke} />
        )}
        {urls.publicRegistry && (
          <CopyValueField label="Public registry" value={urls.publicRegistry} />
        )}
      </div>

      {newToken && (
        <div className="mt-3 rounded-lg border border-signal-live/45 bg-signal-live/12 p-3">
          <div className="text-xs font-semibold text-signal-live">New token</div>
          <div className="mt-2 grid gap-2">
            <CopyValueField label="Bearer token" value={newToken.token} />
            {sampleCommand && (
              <CopyValueField label="curl" value={sampleCommand} multiline />
            )}
          </div>
        </div>
      )}

      <SurfacePanel as="section" className="mt-4 bg-runtime-bg/70 p-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-[10px] uppercase text-ink-muted">
              Integration links
            </div>
            <div className="mt-1 text-xs text-ink-muted">
              A token for MCP clients and direct API callers. No OAuth setup is
              required; calls run as your account.{" "}
              {!urlToken && (
                <>
                  Send it as{" "}
                  <span className="font-mono">
                    Authorization: Bearer &lt;token&gt;
                  </span>
                  .{" "}
                </>
              )}
              {integrationLinkUrlSecrecyNote(urlToken)}
            </div>
          </div>
          <ToolbarButton
            onClick={mintIntegrationLink}
            disabled={Boolean(busy)}
            variant="primary"
            size="md"
          >
            {busy === "link" ? "Generating..." : "Generate link"}
          </ToolbarButton>
        </div>

        <label className="mt-3 flex items-start gap-2 text-[11px] text-ink-muted">
          <input
            type="checkbox"
            checked={urlToken}
            onChange={(event) => setUrlToken(event.target.checked)}
            disabled={Boolean(busy)}
            className="mt-0.5"
          />
          <span>
            Put the token in the URL instead (lower security) — only for MCP or
            OpenAPI clients that cannot send a header. URL tokens leak through
            access logs, browser history and Referer headers, so they are given a
            shorter lifetime.
          </span>
        </label>

        {newLink && (
          <div className="mt-3 rounded-lg border border-signal-live/45 bg-signal-live/12 p-3">
            <div className="text-xs font-semibold text-signal-live">
              New integration link
            </div>
            {newLink.security_notice && (
              <InlineAlert tone="amber" className="mt-2 text-[11px]">
                {newLink.security_notice}
              </InlineAlert>
            )}
            <div className="mt-2 grid gap-2">
              <CopyValueField label="Bearer token" value={newLink.token} />
              <CopyValueField label="curl" value={newLink.curl_example} multiline />
              <CopyValueField
                label={integrationLinkUrlLabel("OpenAPI URL", newLink)}
                value={newLink.openapi_url}
              />
              <CopyValueField
                label={integrationLinkUrlLabel("MCP URL", newLink)}
                value={newLink.mcp_url}
              />
              {newLink.sample_invoke_url && (
                <CopyValueField
                  label={integrationLinkUrlLabel("Sample invoke URL", newLink)}
                  value={newLink.sample_invoke_url}
                />
              )}
            </div>
            {newLink.expires_at && (
              <div className="mt-2 text-[11px] text-ink-muted">
                Expires {fmtDate(newLink.expires_at)}. Generate a new link before
                then; delete this one to revoke it immediately.
              </div>
            )}
          </div>
        )}

        <div className="mt-3">
          {links === null ? (
            <SurfacePanel as="div" className="bg-runtime-panel/40 px-3 py-3 text-xs text-ink-muted">
              Loading integration links...
            </SurfacePanel>
          ) : links.length === 0 ? (
            <SurfacePanel as="div" className="bg-runtime-panel/40 px-3 py-3 text-xs text-ink-muted">
              No integration links generated.
            </SurfacePanel>
          ) : (
            <div className="grid gap-2">
              {links.map((link) => (
                <SurfacePanel
                  as="article"
                  key={link.id}
                  className="flex flex-col gap-2 bg-runtime-panel/40 px-3 py-2 text-xs lg:flex-row lg:items-center lg:justify-between"
                >
                  <div className="min-w-0">
                    <div className="truncate text-ink-soft">Integration link</div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-muted">
                      <span className="font-mono">...{link.token_last4}</span>
                      <span>{link.enabled ? "enabled" : "revoked"}</span>
                      <span>
                        {link.expires_at
                          ? `expires ${fmtDate(link.expires_at)}`
                          : "no expiry"}
                      </span>
                      <span>{link.last_used_at ? `used ${fmtDate(link.last_used_at)}` : "never used"}</span>
                    </div>
                  </div>
                  <ToolbarButton
                    onClick={() => revokeLink(link)}
                    disabled={!link.enabled || Boolean(busy)}
                    variant="danger"
                  >
                    {busy === `link:${link.id}` ? "Deleting..." : "Delete"}
                  </ToolbarButton>
                </SurfacePanel>
              ))}
            </div>
          )}
        </div>
      </SurfacePanel>

      <div className="mt-3">
        <div className="text-[10px] uppercase text-ink-faint">Tokens</div>
        {tokens === null ? (
          <SurfacePanel as="div" className="mt-2 bg-runtime-panel/40 px-3 py-3 text-xs text-ink-muted">
            Loading tokens...
          </SurfacePanel>
        ) : tokens.length === 0 ? (
          <SurfacePanel as="div" className="mt-2 bg-runtime-panel/40 px-3 py-3 text-xs text-ink-muted">
            No API tokens minted.
          </SurfacePanel>
        ) : (
          <div className="mt-2 grid gap-2">
            {tokens.map((token) => (
              <SurfacePanel
                as="article"
                key={token.id}
                className="flex flex-col gap-2 bg-runtime-panel/40 px-3 py-2 text-xs sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="truncate text-ink-soft">{token.name}</div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-muted">
                    <span className="font-mono">...{token.token_last4}</span>
                    <span>{token.enabled ? "enabled" : "revoked"}</span>
                    <span>{token.last_used_at ? `used ${fmtDate(token.last_used_at)}` : "never used"}</span>
                  </div>
                </div>
                <ToolbarButton
                  onClick={() => revoke(token)}
                  disabled={!token.enabled || Boolean(busy)}
                  variant="danger"
                >
                  {busy === `revoke:${token.id}` ? "Revoking..." : "Revoke"}
                </ToolbarButton>
              </SurfacePanel>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function sampleCurl({ url, token }: { url: string; token: string }) {
  const body = '{\"arguments\":{}}';
  return [
    `curl -X POST ${JSON.stringify(url)} \\`,
    `  -H ${JSON.stringify(`Authorization: Bearer ${token}`)} \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${JSON.stringify(body)}`,
  ].join("\n");
}
