import { PurgeAllButton } from "@/components/PurgeAllButton";
import { FeatureFlagsPanel } from "@/components/FeatureFlagsPanel";
import { UserAccessForm } from "@/components/UserAccessForm";
import { UserFeatureFlagsForm } from "@/components/UserFeatureFlagsForm";
import { UserPolicyForm } from "@/components/UserPolicyForm";
import { sections } from "@/lib/admin-data";
import { listAdminFeatureFlags, listAdminUsers } from "@/lib/cp";

export const dynamic = "force-dynamic";

export default async function UsersPage() {
  let inventory: Awaited<ReturnType<typeof listAdminUsers>> | null = null;
  let featureFlags: Awaited<ReturnType<typeof listAdminFeatureFlags>> = [];
  let loadError: string | null = null;
  try {
    [inventory, featureFlags] = await Promise.all([
      listAdminUsers(),
      listAdminFeatureFlags(),
    ]);
  } catch (e) {
    loadError = e instanceof Error ? e.message : String(e);
  }

  const section = sections.users;
  const totals = inventory?.totals ?? { users: 0, agents: 0, repos: 0 };
  const signups = inventory?.signups ?? { last_24h: 0, last_7d: 0, last_30d: 0 };

  return (
    <div className="space-y-6">
      <header className="border-b border-line pb-5" data-tour="page-title">
        <div className="text-xs uppercase text-neutral-500">{section.eyebrow}</div>
        <h1 className="mt-2 text-3xl font-semibold text-neutral-50">{section.title}</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-400">{section.description}</p>
      </header>

      {loadError && (
        <div className="rounded-md border border-red-800/60 bg-red-950/30 px-4 py-3 text-sm text-red-200">
          Could not reach the control plane: {loadError}
        </div>
      )}

      <section className="grid gap-3 md:grid-cols-3 xl:grid-cols-6" aria-label="User inventory totals">
        <MetricCard label="Users" value={totals.users.toString()} detail="All control-plane accounts" />
        <MetricCard label="24h signups" value={signups.last_24h.toString()} detail="New accounts since yesterday" />
        <MetricCard label="7d signups" value={signups.last_7d.toString()} detail="New accounts this week" />
        <MetricCard label="30d signups" value={signups.last_30d.toString()} detail="New accounts this month" />
        <MetricCard label="Agents" value={totals.agents.toString()} detail="Owned agent rows" />
        <MetricCard label="Repos" value={totals.repos.toString()} detail="Managed source repos" />
      </section>

      <section className="rounded-lg border border-line bg-panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-3xl">
            <div className="text-sm font-semibold text-neutral-100">Delete all data</div>
            <p className="mt-2 text-sm leading-6 text-neutral-400">
              This removes every user, agent, and managed repository record from the control plane, and
              cleans up the underlying managed agent repos before the database wipe. Platform settings stay
              in place.
            </p>
          </div>
          <PurgeAllButton
            disabled={!inventory || totals.users === 0}
            userCount={totals.users}
            agentCount={totals.agents}
            repoCount={totals.repos}
          />
        </div>
      </section>

      <FeatureFlagsPanel initialFlags={featureFlags} />

      <section className="rounded-lg border border-line bg-panel" aria-label="Users table">
        <div className="grid grid-cols-[minmax(0,1fr)_70px_70px_70px_minmax(260px,1fr)_minmax(250px,0.9fr)_minmax(320px,1.2fr)_160px] gap-3 border-b border-line px-4 py-3 text-xs uppercase text-neutral-500">
          <div>User</div>
          <div>Admin</div>
          <div>Agents</div>
          <div>Repos</div>
          <div>Access</div>
          <div>Flags</div>
          <div>Policy</div>
          <div>Created</div>
        </div>
        <div className="divide-y divide-line">
          {inventory?.users.length ? (
            inventory.users.map((user) => (
              <div
                key={user.id}
                className="grid grid-cols-1 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1fr)_70px_70px_70px_minmax(260px,1fr)_minmax(250px,0.9fr)_minmax(320px,1.2fr)_160px]"
              >
                <div className="min-w-0">
                  <div className="truncate font-medium text-neutral-100">{user.email}</div>
                  <div className="mt-1 text-xs text-neutral-500">User ID {user.id}</div>
                </div>
                <div>
                  <span
                    className={`inline-flex rounded-md border px-2 py-1 text-xs font-medium ${
                      user.is_admin
                        ? "border-emerald-700/50 bg-emerald-950/30 text-emerald-200"
                        : "border-neutral-800 bg-neutral-950 text-neutral-400"
                    }`}
                  >
                    {user.is_admin ? "Yes" : "No"}
                  </span>
                </div>
                <div className="text-sm text-neutral-300">{user.agent_count}</div>
                <div className="text-sm text-neutral-300">{user.repo_count}</div>
                <UserAccessForm userId={user.id} />
                <UserFeatureFlagsForm
                  userId={user.id}
                  availableFlags={featureFlags}
                  initialEnabledKeys={user.feature_flags}
                />
                <UserPolicyForm userId={user.id} initial={user.control_policy} />
                <div className="text-sm text-neutral-500">
                  {new Date(user.created_at).toLocaleString()}
                </div>
              </div>
            ))
          ) : (
            <div className="px-4 py-8 text-sm text-neutral-500">
              No users found.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function MetricCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-panel p-4">
      <div className="text-xs text-neutral-500">{label}</div>
      <div className="mt-2 truncate text-lg font-semibold text-neutral-100">{value}</div>
      <div className="mt-1 text-xs leading-5 text-neutral-500">{detail}</div>
    </div>
  );
}
