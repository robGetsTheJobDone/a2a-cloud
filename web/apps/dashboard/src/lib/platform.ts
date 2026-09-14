/**
 * Platform host resolution for the dashboard.
 *
 * The dashboard is served at `app.<domain>`; agents at `<agent>.<domain>`,
 * docs at `docs.<domain>`, the API at `api.<domain>`, auth at `auth.<domain>`.
 * The domain comes from `VITE_A2A_PLATFORM_DOMAIN` at build time, else from
 * `window.location.host` with the `app.` prefix stripped, so a self-hosted
 * build works without any configuration.
 */

type ViteEnv = Record<string, string | undefined> | undefined;

function viteEnv(): ViteEnv {
  try {
    return (import.meta as unknown as { env?: ViteEnv }).env;
  } catch {
    return undefined;
  }
}

function envValue(name: string): string {
  const fromVite = viteEnv()?.[name];
  if (fromVite) return fromVite;
  const fromNode =
    typeof process !== "undefined" ? (process.env as Record<string, string | undefined>)[name] : undefined;
  return fromNode || "";
}

/** Registrable platform domain, or "" when unknown (tests, file:// previews). */
export function platformDomain(): string {
  const configured = envValue("VITE_A2A_PLATFORM_DOMAIN").trim();
  if (configured) return configured;
  const host = globalThis.location?.host || "";
  if (!host || host === "localhost" || /^localhost:\d+$/.test(host) || /^\d+\.\d+\.\d+\.\d+/.test(host)) {
    return "";
  }
  return host.replace(/^app\./, "");
}

export function agentUrl(agentName: string): string {
  const name = encodeURIComponent(agentName);
  const domain = platformDomain();
  return domain ? `https://${name}.${domain}` : `/agents/${name}`;
}

export function apiUrl(): string {
  const configured = envValue("VITE_A2A_API_URL").trim();
  if (configured) return configured.replace(/\/+$/, "");
  const domain = platformDomain();
  return domain ? `https://api.${domain}` : "";
}

export function docsUrl(path = ""): string {
  const configured = envValue("VITE_A2A_DOCS_URL").trim();
  const domain = platformDomain();
  const base = configured || (domain ? `https://docs.${domain}` : "");
  if (!base) return "";
  return path ? `${base.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}` : base;
}

export function keycloakRealm(): string {
  const configured = envValue("VITE_A2A_KEYCLOAK_REALM").trim();
  return configured || "a2a";
}

export function authUrl(path = ""): string {
  const configured = envValue("VITE_A2A_AUTH_URL").trim();
  const domain = platformDomain();
  const base = configured || (domain ? `https://auth.${domain}` : "");
  if (!base) return "";
  return path ? `${base.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}` : base;
}

/** True when `url` points at an agent hosted on this platform. */
export function isPlatformAgentHost(hostname: string): boolean {
  const domain = platformDomain();
  return Boolean(domain) && hostname.endsWith(`.${domain}`);
}
