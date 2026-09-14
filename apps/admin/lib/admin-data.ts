import {
  adminHost,
  apiUrl,
  argocdUrl,
  dashboardUrl,
  giteaUrl,
  grafanaUrl,
  ingressHostTemplate,
  platformDomain,
  registryHost,
} from "./platform";

export type AdminLink = {
  label: string;
  href: string;
};

export type AdminMetric = {
  label: string;
  value: string;
  tone?: "neutral" | "green" | "amber" | "red" | "cyan";
};

export type WorkItem = {
  title: string;
  owner: string;
  state: string;
  detail: string;
  tone?: "neutral" | "green" | "amber" | "red" | "cyan";
};

export type AdminSection = {
  slug: string;
  label: string;
  title: string;
  eyebrow: string;
  description: string;
  metrics: AdminMetric[];
  work: WorkItem[];
  links: AdminLink[];
};

export const navItems = [
  { href: "/", label: "Overview" },
  { href: "/users", label: "Users" },
  { href: "/agents", label: "Agents" },
  { href: "/deployments", label: "Deployments" },
  { href: "/organizations", label: "Organizations" },
  { href: "/platform", label: "Platform" },
  { href: "/secrets", label: "Secrets" },
  { href: "/audit", label: "Audit" },
];

export const sections: Record<string, AdminSection> = {
  users: {
    slug: "users",
    label: "Users",
    eyebrow: "Identity",
    title: "User Inventory",
    description: "All control-plane users, their agent counts, and their managed repository counts.",
    metrics: [
      { label: "Users", value: "all accounts", tone: "cyan" },
      { label: "Agents", value: "per owner" },
      { label: "Repos", value: "managed source" },
      { label: "Purge", value: "one click", tone: "amber" },
    ],
    work: [
      {
        title: "Inspect ownership",
        owner: "Control Plane",
        state: "Live",
        detail: "Review each user's agent count and managed repository footprint before purging anything.",
        tone: "green",
      },
      {
        title: "Confirm cleanup scope",
        owner: "Control Plane",
        state: "Watch",
        detail: "Delete-all removes users, agents, and managed repo records, while leaving platform settings intact.",
        tone: "amber",
      },
      {
        title: "Purge all data",
        owner: "Admin",
        state: "Danger",
        detail: "One click clears the control-plane inventory and the managed agent repos.",
        tone: "red",
      },
    ],
    links: [
      { label: "Control plane", href: `${apiUrl()}/docs` },
      { label: "Gitea", href: `${giteaUrl()}/gitea_admin` },
    ],
  },
  agents: {
    slug: "agents",
    label: "Agents",
    eyebrow: "Runtime",
    title: "Agent Operations",
    description: "Ownership, deployment state, runtime upgrades, public exposure, and teardown work.",
    metrics: [
      { label: "Managed repos", value: "Gitea", tone: "cyan" },
      { label: "Runtime namespace", value: "agents" },
      { label: "Image registry", value: registryHost() },
      { label: "Ingress template", value: ingressHostTemplate() },
    ],
    work: [
      {
        title: "Review owned agents",
        owner: "Dashboard",
        state: "Open",
        detail: "Inventory, runtime upgrade status, live URL, and deployment timeline.",
        tone: "green",
      },
      {
        title: "Confirm public exposure",
        owner: "Kubernetes",
        state: "Watch",
        detail: "Ingress and certificate readiness for each public agent host.",
        tone: "cyan",
      },
      {
        title: "Clean failed builds",
        owner: "Gitea Actions",
        state: "Manual",
        detail: "Inspect failed workflow runs before redeploying source.",
        tone: "amber",
      },
    ],
    links: [
      { label: "My agents", href: `${dashboardUrl()}/my-agents` },
      { label: "Gitea", href: `${giteaUrl()}/gitea_admin` },
      { label: "Registry", href: `https://${registryHost()}` },
    ],
  },
  deployments: {
    slug: "deployments",
    label: "Deployments",
    eyebrow: "Release",
    title: "Deployment Control",
    description: "Argo applications, image tags, rollout health, and build pipeline handoff.",
    metrics: [
      { label: "GitOps owner", value: "Argo CD", tone: "green" },
      { label: "Build owner", value: "Gitea Actions" },
      { label: "Deploy path", value: "deploy/" },
      { label: "Rollout mode", value: "Recreate" },
    ],
    work: [
      {
        title: "Reconcile applications",
        owner: "Argo CD",
        state: "Ready",
        detail: "Sync and health for platform apps, agents, and the admin console.",
        tone: "green",
      },
      {
        title: "Trace image bumps",
        owner: "Gitea Actions",
        state: "Active",
        detail: "CI commits update deploy manifests after pushing images.",
        tone: "cyan",
      },
      {
        title: "Verify certificates",
        owner: "cert-manager",
        state: "Watch",
        detail: `TLS issuance and renewal for ${platformDomain()} hosts.`,
        tone: "amber",
      },
    ],
    links: [
      { label: "Argo CD", href: argocdUrl() },
      { label: "Gitea Actions", href: `${giteaUrl()}/gitea_admin` },
      { label: "Grafana", href: grafanaUrl() },
    ],
  },
  organizations: {
    slug: "organizations",
    label: "Organizations",
    eyebrow: "Identity",
    title: "Organization Admin",
    description: "Members, roles, verified domains, SAML identity providers, and SCIM provisioning.",
    metrics: [
      { label: "Roles", value: "owner/admin/member" },
      { label: "SSO", value: "SAML", tone: "cyan" },
      { label: "Provisioning", value: "SCIM" },
      { label: "Audit scope", value: "org events" },
    ],
    work: [
      {
        title: "Manage members",
        owner: "Control Plane",
        state: "Live",
        detail: "Role changes, deprovisioning, and owner protection.",
        tone: "green",
      },
      {
        title: "Verify domains",
        owner: "DNS",
        state: "Manual",
        detail: "TXT records unlock SAML enforcement for an organization.",
        tone: "amber",
      },
      {
        title: "Inspect SCIM tokens",
        owner: "Control Plane",
        state: "Sensitive",
        detail: "Provisioning tokens and last-used timestamps.",
        tone: "red",
      },
    ],
    links: [
      { label: "Organization", href: `${dashboardUrl()}/organization` },
      { label: "Control API", href: `${apiUrl()}/docs` },
    ],
  },
  secrets: {
    slug: "secrets",
    label: "Secrets",
    eyebrow: "Credentials",
    title: "Credential Operations",
    description: "Admin login, LLM credentials, agent runtime secrets, webhook tokens, and repository credentials.",
    metrics: [
      { label: "Admin auth", value: "admin-auth", tone: "green" },
      { label: "Agent secrets", value: "per-agent" },
      { label: "LLM keys", value: "BYOK" },
      { label: "Repo secrets", value: "Argo/Gitea" },
    ],
    work: [
      {
        title: "Rotate admin login",
        owner: "Kubernetes",
        state: "Available",
        detail: "Update the admin-auth secret without changing app source.",
        tone: "green",
      },
      {
        title: "Review LLM keys",
        owner: "Control Plane",
        state: "Sensitive",
        detail: "Base URL, model, redacted key, and routing defaults.",
        tone: "red",
      },
      {
        title: "Audit agent runtime secrets",
        owner: "Agents namespace",
        state: "Watch",
        detail: "Per-agent secret names are derived and mounted at runtime.",
        tone: "amber",
      },
    ],
    links: [
      { label: "LLM keys", href: `${dashboardUrl()}/keys` },
      { label: "Gitea", href: giteaUrl() },
    ],
  },
  audit: {
    slug: "audit",
    label: "Audit",
    eyebrow: "Receipts",
    title: "Audit And Receipts",
    description: "Control-room receipts, deployment events, organization audit logs, and agent work ledger data.",
    metrics: [
      { label: "Control receipts", value: "enabled", tone: "green" },
      { label: "Deploy events", value: "recorded" },
      { label: "Org logs", value: "tracked" },
      { label: "Work ledger", value: "available" },
    ],
    work: [
      {
        title: "Review spend receipts",
        owner: "Control Room",
        state: "Live",
        detail: "LLM spend, run caps, approvals, and receipt paths.",
        tone: "green",
      },
      {
        title: "Inspect failed handoffs",
        owner: "Subagent runs",
        state: "Active",
        detail: "Errors, grant IDs, file ops, and rerun eligibility.",
        tone: "amber",
      },
      {
        title: "Check organization audit",
        owner: "Organization API",
        state: "Live",
        detail: "SAML, SCIM, member, and domain events.",
        tone: "cyan",
      },
    ],
    links: [
      { label: "Control room", href: `${dashboardUrl()}/control-room` },
      { label: "Activity", href: `${dashboardUrl()}/activity` },
    ],
  },
};

export const overviewMetrics: AdminMetric[] = [
  { label: "Admin app", value: "Next.js", tone: "green" },
  { label: "Auth boundary", value: "Separate", tone: "cyan" },
  { label: "Git source", value: "apps/admin" },
  { label: "Host", value: adminHost() },
];
