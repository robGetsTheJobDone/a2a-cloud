/**
 * Platform host resolution. Every external URL the admin console renders is
 * derived from one domain so a self-hosted install never points at someone
 * else's infrastructure.
 *
 *   A2A_PLATFORM_DOMAIN=example.com  ->  api.example.com, app.example.com, ...
 *
 * Individual hosts can still be overridden (A2A_API_URL, A2A_DASHBOARD_URL,
 * A2A_GITEA_URL, A2A_REGISTRY_HOST, A2A_ARGOCD_URL, A2A_GRAFANA_URL).
 */

const DEFAULT_PLATFORM_DOMAIN = "example.com";

function trimSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export function platformDomain(): string {
  return (
    process.env.NEXT_PUBLIC_A2A_PLATFORM_DOMAIN ||
    process.env.A2A_PLATFORM_DOMAIN ||
    DEFAULT_PLATFORM_DOMAIN
  ).trim();
}

export function apiUrl(): string {
  return trimSlash(process.env.A2A_API_URL || `https://api.${platformDomain()}`);
}

export function dashboardUrl(): string {
  return trimSlash(process.env.A2A_DASHBOARD_URL || `https://app.${platformDomain()}`);
}

export function giteaUrl(): string {
  return trimSlash(process.env.A2A_GITEA_URL || `https://gitea.${platformDomain()}`);
}

export function registryHost(): string {
  return process.env.A2A_REGISTRY_HOST || `registry.${platformDomain()}`;
}

export function argocdUrl(): string {
  return trimSlash(process.env.A2A_ARGOCD_URL || `https://argocd.${platformDomain()}`);
}

export function grafanaUrl(): string {
  return trimSlash(process.env.A2A_GRAFANA_URL || `https://grafana.${platformDomain()}`);
}

export function adminHost(): string {
  return process.env.ADMIN_PUBLIC_URL
    ? process.env.ADMIN_PUBLIC_URL.replace(/^https?:\/\//, "").replace(/\/+$/, "")
    : `admin.${platformDomain()}`;
}

export function ingressHostTemplate(): string {
  return process.env.A2A_INGRESS_HOST_TEMPLATE || `{name}.${platformDomain()}`;
}
