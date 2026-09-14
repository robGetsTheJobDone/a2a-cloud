export function publicAgentUrl(agentName: string): string {
  // Agents are served on their own subdomain of the platform domain
  // (app.<domain> -> <agent>.<domain>). Falls back to a relative path in
  // environments without a window (tests).
  const host = globalThis.location?.host || "";
  const domain = host.replace(/^app\./, "");
  const name = encodeURIComponent(agentName);
  return domain ? `https://${name}.${domain}` : `/agents/${name}`;
}
