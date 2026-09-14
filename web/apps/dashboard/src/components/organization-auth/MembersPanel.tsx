import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { type Organization, type OrganizationMember } from "../../api";
import {
  CopyValueRow,
  EmptyState,
  InlineAlert,
  SectionPanel,
  SelectableSurfaceLink,
  SummaryMetric,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { DetailSheet } from "../ListDetailLayout";
import { formatDate, organizationMemberRoute, organizationRoute, roleTone } from "./shared";

/**
 * MembersPanel — organization member roster. The per-member detail (mandate C)
 * opens as an in-place right-side DetailSheet driven by the route segment.
 */
export function MembersPanel({
  org,
  members,
  selectedMemberId,
}: {
  org: Organization;
  members: OrganizationMember[];
  selectedMemberId: string | null;
}) {
  const navigate = useNavigate();
  const selectedMember = useMemo(
    () =>
      selectedMemberId
        ? members.find((member) => String(member.id) === selectedMemberId) ?? null
        : null,
    [members, selectedMemberId],
  );

  const closeSheet = () => navigate(organizationRoute(org.slug, "members"));

  return (
    <>
      <SectionPanel title="Members">
        {members.length === 0 ? (
          <EmptyState title="No members" />
        ) : (
          <div className="space-y-2">
            {members.map((member) => (
              <MemberRow
                key={member.id}
                org={org}
                member={member}
                selected={String(member.id) === selectedMemberId}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <MemberDetail
        org={org}
        open={Boolean(selectedMemberId)}
        onClose={closeSheet}
        member={selectedMember}
        selectedMemberId={selectedMemberId ?? ""}
      />
    </>
  );
}

function MemberRow({
  org,
  member,
  selected = false,
}: {
  org: Organization;
  member: OrganizationMember;
  selected?: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={organizationMemberRoute(org.slug, member.id)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">{member.email}</div>
          <div className="mt-1 flex flex-wrap gap-2 text-xs text-ink-muted">
            <span>{member.external_id || "local"}</span>
            <span>joined {formatDate(member.created_at)}</span>
          </div>
          <div className="mt-1 font-mono text-[11px] text-ink-muted">
            user {member.user_id}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StatusBadge tone={roleTone(member.role)}>{member.role}</StatusBadge>
          <StatusBadge tone={member.active ? "emerald" : "neutral"} dot>
            {member.active ? "active" : "inactive"}
          </StatusBadge>
        </div>
      </div>
      <div className="mt-2 text-[11px] text-ink-soft">{selected ? "Open in detail" : "Open"}</div>
    </SelectableSurfaceLink>
  );
}

function MemberDetail({
  org,
  open,
  onClose,
  member,
  selectedMemberId,
}: {
  org: Organization;
  open: boolean;
  onClose: () => void;
  member: OrganizationMember | null;
  selectedMemberId: string;
}) {
  if (!member) {
    return (
      <DetailSheet open={open} onClose={onClose} title={selectedMemberId} description="Member detail">
        <InlineAlert tone="amber" className="text-xs">
          This member is not in the current organization member list.
        </InlineAlert>
        <ToolbarButton onClick={onClose} className="mt-3">
          Back to members
        </ToolbarButton>
      </DetailSheet>
    );
  }

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title={
        <span className="text-base font-semibold text-ink [overflow-wrap:anywhere]">
          {member.email}
        </span>
      }
      description={`Member of ${org.name}`}
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone={roleTone(member.role)}>{member.role}</StatusBadge>
          <StatusBadge tone={member.active ? "emerald" : "neutral"} dot>
            {member.active ? "active" : "inactive"}
          </StatusBadge>
        </div>
        <div className="grid gap-2 md:grid-cols-2">
          <SummaryMetric label="member id" value={member.id.toString()} size="compact" />
          <SummaryMetric label="user id" value={member.user_id.toString()} size="compact" />
          <SummaryMetric label="role" value={member.role} size="compact" />
          <SummaryMetric label="status" value={member.active ? "active" : "inactive"} size="compact" />
          <SummaryMetric label="joined" value={formatDate(member.created_at)} size="compact" mono={false} />
          <SummaryMetric label="updated" value={formatDate(member.updated_at)} size="compact" mono={false} />
        </div>

        <SectionPanel title="Identity">
          <div className="grid gap-2">
            <CopyValueRow label="email" value={member.email} />
            <CopyValueRow label="external id" value={member.external_id || "local"} />
            <CopyValueRow label="organization" value={org.slug} />
            <CopyValueRow label="role" value={member.role} />
          </div>
        </SectionPanel>
      </div>
    </DetailSheet>
  );
}
