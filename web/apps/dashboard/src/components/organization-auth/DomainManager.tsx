import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { type Organization, type OrganizationDomain } from "../../api";
import {
  CopyValueRow,
  EmptyState,
  InlineAlert,
  SectionPanel,
  SelectableSurfaceLink,
  SummaryMetric,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { DetailSheet } from "../ListDetailLayout";
import { Icon } from "../Icon";
import { formatDate, organizationDomainRoute, organizationRoute } from "./shared";

/**
 * DomainManager — verified-domain list plus the add form. The per-domain detail
 * (mandate C) opens as an in-place right-side DetailSheet driven by the route
 * segment; closing it navigates back to the domains list route.
 */
export function DomainPanel({
  org,
  domains,
  selectedDomainName,
  newDomain,
  busy,
  onNewDomain,
  onAddDomain,
  onVerifyDomain,
  onDeleteDomain,
}: {
  org: Organization;
  domains: OrganizationDomain[];
  selectedDomainName: string | null;
  newDomain: string;
  busy: string | null;
  onNewDomain: (value: string) => void;
  onAddDomain: (event: FormEvent<HTMLFormElement>) => void;
  onVerifyDomain: (domain: string) => void;
  onDeleteDomain: (domain: string) => Promise<boolean>;
}) {
  const navigate = useNavigate();
  const selectedDomain = useMemo(
    () =>
      selectedDomainName
        ? domains.find((domain) => domain.domain === selectedDomainName) ?? null
        : null,
    [domains, selectedDomainName],
  );

  const closeSheet = () => navigate(organizationRoute(org.slug, "domains"));

  return (
    <>
      <SectionPanel title="Verified domains">
        <form className="mb-4 flex gap-2" onSubmit={onAddDomain}>
          <TextInput
            value={newDomain}
            onChange={(event) => onNewDomain(event.target.value)}
            placeholder="acme.com"
            className="min-w-0 flex-1"
          />
          <ToolbarButton
            type="submit"
            size="md"
            variant="primary"
            disabled={busy === "domain:add" || !newDomain.trim()}
          >
            <Icon name="plus" size={15} />
            {busy === "domain:add" ? "Adding..." : "Add"}
          </ToolbarButton>
        </form>
        {domains.length === 0 ? (
          <EmptyState title="No domains" />
        ) : (
          <div className="space-y-2">
            {domains.map((domain) => (
              <OrganizationDomainRow
                key={domain.id}
                org={org}
                domain={domain}
                selected={domain.domain === selectedDomainName}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <OrganizationDomainDetail
        org={org}
        open={Boolean(selectedDomainName)}
        onClose={closeSheet}
        domain={selectedDomain}
        selectedDomainName={selectedDomainName ?? ""}
        busy={busy}
        onVerifyDomain={onVerifyDomain}
        onDeleteDomain={async (domain) => {
          const success = await onDeleteDomain(domain);
          if (success) closeSheet();
          return success;
        }}
      />
    </>
  );
}

function OrganizationDomainRow({
  org,
  domain,
  selected = false,
}: {
  org: Organization;
  domain: OrganizationDomain;
  selected?: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={organizationDomainRoute(org.slug, domain.domain)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-sm text-ink">{domain.domain}</div>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-ink-muted">
            <span>added {formatDate(domain.created_at)}</span>
            <span>verified {formatDate(domain.verified_at)}</span>
          </div>
          <div className="mt-1 truncate font-mono text-[11px] text-ink-dim">
            {domain.verification_record_name}
          </div>
        </div>
        <StatusBadge tone={domain.verified_at ? "emerald" : "amber"} dot>
          {domain.verified_at ? "verified" : "pending"}
        </StatusBadge>
      </div>
      <div className="mt-2 text-[11px] text-ink-soft">{selected ? "Open in detail" : "Open"}</div>
    </SelectableSurfaceLink>
  );
}

function OrganizationDomainDetail({
  org,
  open,
  onClose,
  domain,
  selectedDomainName,
  busy,
  onVerifyDomain,
  onDeleteDomain,
}: {
  org: Organization;
  open: boolean;
  onClose: () => void;
  domain: OrganizationDomain | null;
  selectedDomainName: string;
  busy: string | null;
  onVerifyDomain: (domain: string) => void;
  onDeleteDomain: (domain: string) => Promise<boolean>;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setConfirmDelete(false);
  }, [domain?.domain]);

  if (!domain) {
    return (
      <DetailSheet open={open} onClose={onClose} title={selectedDomainName} description="Domain detail">
        <InlineAlert tone="amber" className="text-xs">
          This domain is not in the current organization domain list.
        </InlineAlert>
        <ToolbarButton onClick={onClose} className="mt-3">
          Back to domains
        </ToolbarButton>
      </DetailSheet>
    );
  }

  const verifying = busy === `domain:verify:${domain.domain}`;
  const deleting = busy === `domain:delete:${domain.domain}`;
  const verified = Boolean(domain.verified_at);

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title={
        <span className="font-mono text-base text-ink [overflow-wrap:anywhere]">
          {domain.domain}
        </span>
      }
      description={`Organization domain for ${org.name}`}
      footer={
        <div className="flex flex-wrap gap-2">
          {!verified && (
            <ToolbarButton
              onClick={() => onVerifyDomain(domain.domain)}
              disabled={Boolean(busy)}
            >
              <Icon name="check" size={14} />
              {verifying ? "Verifying..." : "Verify"}
            </ToolbarButton>
          )}
          <ToolbarButton
            variant="danger"
            onClick={() => {
              if (!confirmDelete) {
                setConfirmDelete(true);
                return;
              }
              void onDeleteDomain(domain.domain).then((success) => {
                if (success) setConfirmDelete(false);
              });
            }}
            disabled={Boolean(busy)}
          >
            <Icon name="trash" size={14} />
            {deleting ? "Deleting..." : confirmDelete ? "Confirm delete" : "Delete"}
          </ToolbarButton>
          {confirmDelete && (
            <ToolbarButton onClick={() => setConfirmDelete(false)} disabled={Boolean(busy)}>
              Cancel
            </ToolbarButton>
          )}
        </div>
      }
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={verified ? "emerald" : "amber"} dot>
            {verified ? "verified" : "pending"}
          </StatusBadge>
        </div>
        {confirmDelete && (
          <InlineAlert tone="amber" className="text-xs">
            Deleting this domain removes it from organization identity matching.
          </InlineAlert>
        )}
        <div className="grid gap-2 md:grid-cols-2">
          <SummaryMetric label="status" value={verified ? "verified" : "pending"} size="compact" />
          <SummaryMetric label="created" value={formatDate(domain.created_at)} size="compact" mono={false} />
          <SummaryMetric label="verified" value={formatDate(domain.verified_at)} size="compact" mono={false} />
          <SummaryMetric label="domain id" value={domain.id.toString()} size="compact" />
        </div>

        <SectionPanel
          title="Verification record"
          description="Publish this TXT record in DNS, then verify the domain."
        >
          <div className="grid gap-2">
            <CopyValueRow label="TXT name" value={domain.verification_record_name} />
            <CopyValueRow label="TXT value" value={domain.verification_record_value} />
          </div>
        </SectionPanel>
      </div>
    </DetailSheet>
  );
}
