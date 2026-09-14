import { type FormEvent } from "react";
import {
  type Organization,
  type OrganizationAuditLog,
  type OrganizationDomain,
  type OrganizationMember,
  type OrganizationScimConfig,
  type OrganizationScimToken,
} from "../../api";
import { type OrganizationViewId } from "../../navigation";
import { AuditPanel } from "./AuditLog";
import { DomainPanel } from "./DomainManager";
import { MembersPanel } from "./MembersPanel";
import { OrganizationOverview } from "./OrganizationOverview";
import { ScimPanel } from "./ScimManager";

/**
 * OrganizationDetailView — routes the active organization view to its panel.
 * Each detail panel opens its per-resource detail/edit in an in-place right-side
 * DetailSheet (mandate C) driven by the same route segments as before.
 */
export function OrganizationDetailView({
  org,
  view,
  domains,
  members,
  auditLogs,
  scimConfig,
  scimTokens,
  selectedDomainName,
  selectedScimTokenId,
  selectedAuditLogId,
  selectedMemberId,
  newDomain,
  scimLabel,
  busy,
  createdToken,
  verifiedDomainCount,
  activeScimTokenCount,
  onNewDomain,
  onAddDomain,
  onVerifyDomain,
  onDeleteDomain,
  onScimLabel,
  onCreateScimToken,
  onRevokeScimToken,
}: {
  org: Organization;
  view: OrganizationViewId;
  domains: OrganizationDomain[];
  members: OrganizationMember[];
  auditLogs: OrganizationAuditLog[];
  scimConfig: OrganizationScimConfig | null;
  scimTokens: OrganizationScimToken[];
  selectedDomainName: string | null;
  selectedScimTokenId: string | null;
  selectedAuditLogId: string | null;
  selectedMemberId: string | null;
  newDomain: string;
  scimLabel: string;
  busy: string | null;
  createdToken: string | null;
  verifiedDomainCount: number;
  activeScimTokenCount: number;
  onNewDomain: (value: string) => void;
  onAddDomain: (event: FormEvent<HTMLFormElement>) => void;
  onVerifyDomain: (domain: string) => void;
  onDeleteDomain: (domain: string) => Promise<boolean>;
  onScimLabel: (value: string) => void;
  onCreateScimToken: (event: FormEvent<HTMLFormElement>) => void;
  onRevokeScimToken: (tokenId: number) => Promise<boolean>;
}) {
  if (view === "domains") {
    return (
      <DomainPanel
        org={org}
        domains={domains}
        selectedDomainName={selectedDomainName}
        newDomain={newDomain}
        busy={busy}
        onNewDomain={onNewDomain}
        onAddDomain={onAddDomain}
        onVerifyDomain={onVerifyDomain}
        onDeleteDomain={onDeleteDomain}
      />
    );
  }
  if (view === "scim") {
    return (
      <ScimPanel
        org={org}
        config={scimConfig}
        tokens={scimTokens}
        selectedTokenId={selectedScimTokenId}
        label={scimLabel}
        busy={busy}
        createdToken={createdToken}
        onLabel={onScimLabel}
        onCreate={onCreateScimToken}
        onRevoke={onRevokeScimToken}
      />
    );
  }
  if (view === "members") {
    return <MembersPanel org={org} members={members} selectedMemberId={selectedMemberId} />;
  }
  if (view === "audit") {
    return <AuditPanel org={org} logs={auditLogs} selectedLogId={selectedAuditLogId} />;
  }
  return (
    <OrganizationOverview
      org={org}
      domains={domains}
      members={members}
      auditLogs={auditLogs}
      verifiedDomainCount={verifiedDomainCount}
      activeScimTokenCount={activeScimTokenCount}
    />
  );
}
