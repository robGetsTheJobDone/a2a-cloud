import {
  getAnalyticsProperties,
  getFingerprintId,
  type PostHogClient,
} from "@a2a/analytics/browser";

type AnalyticsUser = {
  id: number;
  email: string;
};

const SURFACE = "app";
const ENVIRONMENT = "production";

declare global {
  interface Window {
    posthog?: PostHogClient;
  }
}

let currentProfileId: string | null = null;
let currentUserId: string | null = null;

function attributionProperties() {
  const fingerprintId = getFingerprintId();
  return {
    ...getAnalyticsProperties({
      surface: SURFACE,
      environment: ENVIRONMENT,
      identityState: currentUserId ? "authenticated" : "anonymous",
      ...(currentUserId ? { userId: currentUserId } : {}),
    }),
    ...(fingerprintId ? { a2aFingerprintId: fingerprintId } : {}),
  };
}

function setGlobalAttribution() {
  if (typeof window === "undefined" || !window.posthog) return;
  window.posthog.register(attributionProperties());
}


export function trackEvent(name: string, properties: Record<string, unknown> = {}) {
  if (typeof window === "undefined" || !window.posthog) return;
  window.posthog.capture(name, {
    ...attributionProperties(),
    ...properties,
  });
}

export function identifyAnalytics(user: AnalyticsUser) {
  if (typeof window === "undefined" || !window.posthog) return;
  const profileId = `user:${user.id}`;
  if (currentProfileId && currentProfileId !== profileId) {
    window.posthog.reset();
  }
  currentProfileId = profileId;
  currentUserId = profileId;
  setGlobalAttribution();
  const fingerprintId = getFingerprintId();
  window.posthog.identify(profileId, {
    ...attributionProperties(),
    email: user.email,
    userId: String(user.id),
    // Join key to the marketing fp:<id> person.
    ...(fingerprintId ? { fingerprintId } : {}),
  });
}

export function clearAnalytics() {
  if (typeof window === "undefined" || !window.posthog) return;
  currentProfileId = null;
  currentUserId = null;
  window.posthog.reset();
  setGlobalAttribution();
}
