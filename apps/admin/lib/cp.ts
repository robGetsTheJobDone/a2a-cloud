// Server-side helper for proxying admin actions to the control plane.
// Keeps the shared admin token off the client; all CP calls go through
// Next.js server functions / API routes that read it from process.env.

const CP_URL =
  process.env.A2A_CP_URL ||
  "http://control-plane.control-plane.svc.cluster.local";

export type PlatformSetting = {
  key: string;
  value: unknown;
  description: string | null;
  updated_by: string | null;
  updated_at: string | null;
};

export type AdminUser = {
  id: number;
  email: string;
  is_admin: boolean;
  created_at: string;
  agent_count: number;
  repo_count: number;
  control_policy: ControlPolicy | null;
  feature_flags: string[];
};

export type ControlPolicy = {
  monthly_budget_cents: number;
  run_budget_cents: number;
  max_agent_calls_per_run: number;
  require_approval_for_file_writes: boolean;
  deny_external_network: boolean;
  only_approved_agents: boolean;
  pii_safe_mode: boolean;
  approved_agents: string[];
};

export type AdminControlPolicy = {
  user_id: number;
  email: string;
  policy: ControlPolicy;
};

export type AdminInventoryTotals = {
  users: number;
  agents: number;
  repos: number;
};

export type AdminSignupTotals = {
  last_24h: number;
  last_7d: number;
  last_30d: number;
};

export type AdminInventory = {
  totals: AdminInventoryTotals;
  signups: AdminSignupTotals;
  users: AdminUser[];
};

export type AdminPurgeResult = {
  deleted_users: number;
  deleted_agents: number;
};

export type AdminKeycloakUser = {
  id: number;
  email: string;
  is_admin: boolean;
};

export type AdminFeatureFlag = {
  key: string;
  label: string;
  description: string | null;
  default_enabled: boolean;
  assigned_user_count: number;
  created_at: string;
  updated_at: string;
};

export type AdminFeatureFlagInput = {
  key: string;
  label: string;
  description: string | null;
  default_enabled: boolean;
};

export type AdminUserFeatureFlags = {
  user_id: number;
  email: string;
  enabled_keys: string[];
  available_flags: AdminFeatureFlag[];
};

export type AdminPlatformToken = {
  user_id: number;
  email: string;
  token_type: "Bearer";
  token: string;
  expires_at: string;
  ttl_seconds: number;
  label: string | null;
};

function adminToken(): string {
  const token = process.env.A2A_CP_ADMIN_TOKEN || "";
  if (!token) {
    throw new Error(
      "A2A_CP_ADMIN_TOKEN is not configured on the admin app environment",
    );
  }
  return token;
}

export async function listPlatformSettings(): Promise<PlatformSetting[]> {
  const resp = await fetch(`${CP_URL}/v1/admin/settings`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    throw new Error(`cp list settings failed: ${resp.status}`);
  }
  return (await resp.json()) as PlatformSetting[];
}

export async function setPlatformSetting(
  key: string,
  value: unknown,
  actor: string | null = null,
): Promise<PlatformSetting> {
  const resp = await fetch(`${CP_URL}/v1/admin/settings/${encodeURIComponent(key)}`, {
    method: "PUT",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ value, actor }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp set setting failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as PlatformSetting;
}

export async function listAdminUsers(): Promise<AdminInventory> {
  const resp = await fetch(`${CP_URL}/v1/admin/users`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    throw new Error(`cp list users failed: ${resp.status}`);
  }
  return (await resp.json()) as AdminInventory;
}

export async function purgeAdminUsers(): Promise<AdminPurgeResult> {
  const resp = await fetch(`${CP_URL}/v1/admin/users`, {
    method: "DELETE",
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp purge users failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminPurgeResult;
}

export async function getAdminUserPolicy(userId: number): Promise<AdminControlPolicy> {
  const resp = await fetch(`${CP_URL}/v1/admin/users/${userId}/control-policy`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    throw new Error(`cp get user policy failed: ${resp.status}`);
  }
  return (await resp.json()) as AdminControlPolicy;
}

export async function setAdminUserPolicy(
  userId: number,
  policy: Partial<ControlPolicy>,
): Promise<AdminControlPolicy> {
  const resp = await fetch(`${CP_URL}/v1/admin/users/${userId}/control-policy`, {
    method: "PUT",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(policy),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp set user policy failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminControlPolicy;
}


export async function createAdminUserPlatformToken(
  userId: number,
  input: { ttl_seconds?: number; label?: string | null },
): Promise<AdminPlatformToken> {
  const resp = await fetch(`${CP_URL}/v1/admin/users/${userId}/platform-token`, {
    method: "POST",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(input),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp create platform token failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminPlatformToken;
}

export async function listAdminFeatureFlags(): Promise<AdminFeatureFlag[]> {
  const resp = await fetch(`${CP_URL}/v1/admin/feature-flags`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    throw new Error(`cp list feature flags failed: ${resp.status}`);
  }
  return (await resp.json()) as AdminFeatureFlag[];
}

export async function upsertAdminFeatureFlag(
  input: AdminFeatureFlagInput,
): Promise<AdminFeatureFlag> {
  const resp = await fetch(`${CP_URL}/v1/admin/feature-flags`, {
    method: "POST",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(input),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp upsert feature flag failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminFeatureFlag;
}

export async function deleteAdminFeatureFlag(key: string): Promise<void> {
  const resp = await fetch(`${CP_URL}/v1/admin/feature-flags/${encodeURIComponent(key)}`, {
    method: "DELETE",
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp delete feature flag failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
}

export async function getAdminUserFeatureFlags(
  userId: number,
): Promise<AdminUserFeatureFlags> {
  const resp = await fetch(`${CP_URL}/v1/admin/users/${userId}/feature-flags`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${adminToken()}` },
  });
  if (!resp.ok) {
    throw new Error(`cp get user feature flags failed: ${resp.status}`);
  }
  return (await resp.json()) as AdminUserFeatureFlags;
}

export async function setAdminUserFeatureFlags(
  userId: number,
  enabledKeys: string[],
): Promise<AdminUserFeatureFlags> {
  const resp = await fetch(`${CP_URL}/v1/admin/users/${userId}/feature-flags`, {
    method: "PUT",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ enabled_keys: enabledKeys }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp set user feature flags failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminUserFeatureFlags;
}

export async function authenticateKeycloakAdmin(idToken: string): Promise<AdminKeycloakUser> {
  const resp = await fetch(`${CP_URL}/v1/admin/auth/keycloak`, {
    method: "POST",
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${adminToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ id_token: idToken }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`cp admin keycloak auth failed: ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (await resp.json()) as AdminKeycloakUser;
}
