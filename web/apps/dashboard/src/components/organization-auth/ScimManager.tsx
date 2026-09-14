import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  type Organization,
  type OrganizationScimConfig,
  type OrganizationScimToken,
} from "../../api";
import {
  CopyValueRow,
  EmptyState,
  InlineAlert,
  SectionPanel,
  SelectableSurfaceLink,
  SummaryMetric,
  SurfacePanel,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { DetailSheet } from "../ListDetailLayout";
import { Icon } from "../Icon";
import { formatDate, organizationRoute, organizationScimTokenRoute } from "./shared";

/**
 * ScimManager — SCIM config + token list with the create form. The per-token
 * detail (mandate C) opens as an in-place right-side DetailSheet driven by the
 * route segment; closing it returns to the scim list route.
 */
export function ScimPanel({
  org,
  config,
  tokens,
  selectedTokenId,
  label,
  busy,
  createdToken,
  onLabel,
  onCreate,
  onRevoke,
}: {
  org: Organization;
  config: OrganizationScimConfig | null;
  tokens: OrganizationScimToken[];
  selectedTokenId: string | null;
  label: string;
  busy: string | null;
  createdToken: string | null;
  onLabel: (value: string) => void;
  onCreate: (event: FormEvent<HTMLFormElement>) => void;
  onRevoke: (tokenId: number) => Promise<boolean>;
}) {
  const navigate = useNavigate();
  const selectedToken = useMemo(
    () =>
      selectedTokenId
        ? tokens.find((token) => String(token.id) === selectedTokenId) ?? null
        : null,
    [tokens, selectedTokenId],
  );

  const closeSheet = () => navigate(organizationRoute(org.slug, "scim"));

  return (
    <>
      <SectionPanel title="SCIM provisioning">
        {config && (
          <div className="mb-4 space-y-2">
            <CopyValueRow label="Base URL" value={config.base_url} />
            <CopyValueRow label="Users URL" value={config.users_url} />
          </div>
        )}
        <form className="mb-4 flex gap-2" onSubmit={onCreate}>
          <TextInput
            value={label}
            onChange={(event) => onLabel(event.target.value)}
            className="min-w-0 flex-1"
          />
          <ToolbarButton
            type="submit"
            size="md"
            variant="primary"
            disabled={busy === "scim:create" || !label.trim()}
          >
            <Icon name="key" size={15} />
            {busy === "scim:create" ? "Creating..." : "New token"}
          </ToolbarButton>
        </form>
        {createdToken && (
          <SurfacePanel as="div" className="mb-4 border-signal-live/45 bg-signal-live/12 p-3">
            <CopyValueRow label="Bearer token" value={createdToken} />
          </SurfacePanel>
        )}
        {tokens.length === 0 ? (
          <EmptyState title="No SCIM tokens" />
        ) : (
          <div className="space-y-2">
            {tokens.map((token) => (
              <ScimTokenRow
                key={token.id}
                org={org}
                token={token}
                selected={String(token.id) === selectedTokenId}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <ScimTokenDetail
        open={Boolean(selectedTokenId)}
        onClose={closeSheet}
        config={config}
        token={selectedToken}
        selectedTokenId={selectedTokenId ?? ""}
        busy={busy}
        onRevoke={onRevoke}
      />
    </>
  );
}

function ScimTokenRow({
  org,
  token,
  selected = false,
}: {
  org: Organization;
  token: OrganizationScimToken;
  selected?: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={organizationScimTokenRoute(org.slug, token.id)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">{token.label}</div>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-ink-muted">
            <span>ends {token.token_last4}</span>
            <span>last used {formatDate(token.last_used_at)}</span>
          </div>
          <div className="mt-1 truncate text-[11px] text-ink-muted">
            created by {token.created_by_email || "unknown"}
          </div>
        </div>
        <StatusBadge tone={token.enabled ? "emerald" : "neutral"} dot>
          {token.enabled ? "enabled" : "revoked"}
        </StatusBadge>
      </div>
      <div className="mt-2 text-[11px] text-ink-soft">{selected ? "Open in detail" : "Open"}</div>
    </SelectableSurfaceLink>
  );
}

function ScimTokenDetail({
  open,
  onClose,
  config,
  token,
  selectedTokenId,
  busy,
  onRevoke,
}: {
  open: boolean;
  onClose: () => void;
  config: OrganizationScimConfig | null;
  token: OrganizationScimToken | null;
  selectedTokenId: string;
  busy: string | null;
  onRevoke: (tokenId: number) => Promise<boolean>;
}) {
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  useEffect(() => {
    setConfirmRevoke(false);
  }, [token?.id]);

  if (!token) {
    return (
      <DetailSheet open={open} onClose={onClose} title={selectedTokenId} description="SCIM token detail">
        <InlineAlert tone="amber" className="text-xs">
          This token is not in the current SCIM token list.
        </InlineAlert>
        <ToolbarButton onClick={onClose} className="mt-3">
          Back to SCIM tokens
        </ToolbarButton>
      </DetailSheet>
    );
  }

  const revoking = busy === `scim:revoke:${token.id}`;

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title={
        <span className="text-base font-semibold text-ink [overflow-wrap:anywhere]">
          {token.label}
        </span>
      }
      description={`token id ${token.id}`}
      footer={
        <div className="flex flex-wrap gap-2">
          {token.enabled && (
            <ToolbarButton
              variant="danger"
              onClick={() => {
                if (!confirmRevoke) {
                  setConfirmRevoke(true);
                  return;
                }
                void onRevoke(token.id).then((success) => {
                  if (success) setConfirmRevoke(false);
                });
              }}
              disabled={Boolean(busy)}
            >
              <Icon name="x" size={14} />
              {revoking ? "Revoking..." : confirmRevoke ? "Confirm revoke" : "Revoke"}
            </ToolbarButton>
          )}
          {confirmRevoke && (
            <ToolbarButton onClick={() => setConfirmRevoke(false)} disabled={Boolean(busy)}>
              Cancel
            </ToolbarButton>
          )}
        </div>
      }
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={token.enabled ? "emerald" : "neutral"} dot>
            {token.enabled ? "enabled" : "revoked"}
          </StatusBadge>
        </div>
        {confirmRevoke && (
          <InlineAlert tone="amber" className="text-xs">
            Revoking this token immediately stops SCIM clients that use it.
          </InlineAlert>
        )}
        <div className="grid gap-2 md:grid-cols-2">
          <SummaryMetric label="status" value={token.enabled ? "enabled" : "revoked"} size="compact" />
          <SummaryMetric label="last four" value={token.token_last4} size="compact" />
          <SummaryMetric label="last used" value={formatDate(token.last_used_at)} size="compact" mono={false} />
          <SummaryMetric label="created" value={formatDate(token.created_at)} size="compact" mono={false} />
          <SummaryMetric label="created by" value={token.created_by_email || "unknown"} size="compact" />
        </div>

        {config && (
          <SectionPanel
            title="Provisioning endpoints"
            description="Use these URLs in the identity provider SCIM application."
          >
            <div className="grid gap-2">
              <CopyValueRow label="Base URL" value={config.base_url} />
              <CopyValueRow
                label="Service provider config"
                value={config.service_provider_config_url}
              />
              <CopyValueRow label="Users URL" value={config.users_url} />
            </div>
          </SectionPanel>
        )}
      </div>
    </DetailSheet>
  );
}
