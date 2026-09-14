export type StudioPrefill = {
  goal: string;
  name: string;
};

const STORAGE_KEY = "a2a:studio-prefill:v1";
const CLAIM_STORAGE_KEY = "a2a:studio-claim:v1";
const NAME_RE = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;
const MAX_GOAL_LENGTH = 800;

function validPrefill(goal: string | null, name: string | null): StudioPrefill | null {
  const cleanGoal = (goal ?? "").trim().slice(0, MAX_GOAL_LENGTH);
  const cleanName = (name ?? "").trim().toLowerCase();
  if (cleanGoal.length < 12 || !NAME_RE.test(cleanName)) return null;
  return { goal: cleanGoal, name: cleanName };
}

/** Capture a funnel brief from the URL fragment before an auth redirect.
 * Fragments never reach HTTP logs or referrer headers; sessionStorage keeps
 * the brief available across the same-tab OIDC round trip. */
export function captureStudioPrefillFromHash(): StudioPrefill | null {
  if (typeof window === "undefined" || !window.location.hash) return null;
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const prefill = validPrefill(params.get("goal"), params.get("name"));
  if (!prefill) return null;

  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(prefill));
  } catch {
    // A logged-in visitor can still use the in-memory return value when
    // storage is disabled; an auth round trip simply cannot retain it.
  }
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  return prefill;
}

export function captureStudioClaimFromHash(): string | null {
  if (typeof window === "undefined" || !window.location.hash) return null;
  const token = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("claim");
  if (!token || !/^[A-Za-z0-9_-]{32,128}$/.test(token)) return null;
  try { window.sessionStorage.setItem(CLAIM_STORAGE_KEY, token); } catch { /* same-tab fallback only */ }
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  return token;
}

export function readStoredStudioClaim(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const token = window.sessionStorage.getItem(CLAIM_STORAGE_KEY);
    return token && /^[A-Za-z0-9_-]{32,128}$/.test(token) ? token : null;
  } catch { return null; }
}

export function clearStoredStudioClaim(): void {
  try { window.sessionStorage.removeItem(CLAIM_STORAGE_KEY); } catch { /* nothing to clear */ }
}

export function readStoredStudioPrefill(): StudioPrefill | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as { goal?: unknown; name?: unknown };
    return validPrefill(
      typeof value.goal === "string" ? value.goal : null,
      typeof value.name === "string" ? value.name : null,
    );
  } catch {
    return null;
  }
}

export function clearStoredStudioPrefill(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage can be disabled; there is nothing else to clear.
  }
}
