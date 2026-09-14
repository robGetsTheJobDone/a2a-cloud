import { type FormEvent } from "react";
import { type Organization } from "../../api";
import {
  EmptyState,
  FormField,
  SectionPanel,
  SelectableSurfaceLink,
  SurfacePanel,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { Icon } from "../Icon";
import { organizationRoute, roleTone } from "./shared";

/**
 * OrganizationList — the left rail: org picker plus the create-organization
 * form. Behavior-preserving extraction of the original aside block.
 */
export function OrganizationList({
  orgs,
  loading,
  routeOrgSlug,
  newOrgName,
  newOrgSlug,
  busy,
  onNewOrgName,
  onNewOrgSlug,
  onCreateOrg,
}: {
  orgs: Organization[];
  loading: boolean;
  routeOrgSlug: string | null;
  newOrgName: string;
  newOrgSlug: string;
  busy: string | null;
  onNewOrgName: (value: string) => void;
  onNewOrgSlug: (value: string) => void;
  onCreateOrg: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <aside className="space-y-4">
      <SectionPanel title="Organizations">
        {loading ? (
          <SurfacePanel as="div" className="bg-runtime-bg px-4 py-5 text-sm text-ink-muted">
            Loading organizations...
          </SurfacePanel>
        ) : orgs.length === 0 ? (
          <EmptyState title="No organizations" />
        ) : (
          <div className="space-y-2">
            {orgs.map((org) => (
              <SelectableSurfaceLink
                key={org.slug}
                href={organizationRoute(org.slug)}
                selected={org.slug === routeOrgSlug}
                className="px-3 py-2"
              >
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate text-sm font-medium text-ink">
                    {org.name}
                  </span>
                  <StatusBadge tone={roleTone(org.role)}>{org.role}</StatusBadge>
                </div>
                <div className="mt-1 truncate text-xs text-ink-muted">{org.slug}</div>
              </SelectableSurfaceLink>
            ))}
          </div>
        )}
      </SectionPanel>

      <SectionPanel title="Create organization">
        <form className="space-y-3" onSubmit={onCreateOrg}>
          <FormField label="Name">
            <TextInput
              value={newOrgName}
              onChange={(event) => onNewOrgName(event.target.value)}
            />
          </FormField>
          <FormField label="Slug">
            <TextInput
              value={newOrgSlug}
              onChange={(event) => onNewOrgSlug(event.target.value)}
            />
          </FormField>
          <ToolbarButton
            type="submit"
            size="md"
            variant="primary"
            disabled={busy === "org:create" || !newOrgName.trim()}
            className="w-full"
          >
            <Icon name="plus" size={15} />
            {busy === "org:create" ? "Creating..." : "Create org"}
          </ToolbarButton>
        </form>
      </SectionPanel>
    </aside>
  );
}
