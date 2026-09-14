/** Subset of the posthog-js client the dashboard actually touches. */
export type PostHogClient = {
  capture: (event: string, properties?: Record<string, unknown>) => void;
  identify: (distinctId: string, properties?: Record<string, unknown>) => void;
  register: (properties: Record<string, unknown>) => void;
  reset: () => void;
};

export type AnalyticsIdentityState = "anonymous" | "authenticated";

export type AttributionTouch = {
  capturedAt: string;
  landingUrl: string;
  landingPath: string;
  referrer: string;
  referrerHost: string;
  source: string;
  medium: string;
  campaign: string;
  term: string;
  content: string;
  id: string;
  gclid: string;
  fbclid: string;
  msclkid: string;
  ttclid: string;
  liFatId: string;
};

export type AttributionOptions = {
  surface: string;
  environment?: string;
  identityState?: AnalyticsIdentityState;
  userId?: string;
};

export const ANONYMOUS_ID_STORAGE_KEY = "a2a.analytics.anonymous_id";
/** Written by the marketing-site loader (posthog.ts) after consent+fp. */
export const FINGERPRINT_COOKIE = "a2a_fp";
export const ATTRIBUTION_FIRST_STORAGE_KEY = "a2a.analytics.attribution.first";
export const ATTRIBUTION_LATEST_STORAGE_KEY = "a2a.analytics.attribution.latest";
export const ATTRIBUTION_COOKIE_FIRST = "a2a_attr_first";
export const ATTRIBUTION_COOKIE_LATEST = "a2a_attr_latest";

export const TRACKED_QUERY_PARAMS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_term",
  "utm_content",
  "utm_id",
  "gclid",
  "fbclid",
  "msclkid",
  "ttclid",
  "li_fat_id",
] as const;

let fallbackAnonymousId: string | null = null;
let attributionCaptured = false;

function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function getAnalyticsAnonymousId(): string {
  if (typeof window === "undefined") {
    fallbackAnonymousId ||= `anon:${randomId()}`;
    return fallbackAnonymousId;
  }
  try {
    const existing = window.localStorage.getItem(ANONYMOUS_ID_STORAGE_KEY);
    if (existing) return existing;
    const next = `anon:${randomId()}`;
    window.localStorage.setItem(ANONYMOUS_ID_STORAGE_KEY, next);
    return next;
  } catch {
    fallbackAnonymousId ||= `anon:${randomId()}`;
    return fallbackAnonymousId;
  }
}

/**
 * Fingerprint id captured on the marketing sites (consent-gated). Empty when
 * the visitor never accepted, declined, or came straight to the app. This is
 * the join key between marketing `fp:*` persons and product `user:*`
 * persons — kept as a person property so the link survives resets.
 */
export function getFingerprintId(): string {
  const value = getCookie(FINGERPRINT_COOKIE);
  try {
    return value ? decodeURIComponent(value) : "";
  } catch {
    return "";
  }
}

function cookieDomain(): string {
  if (typeof window === "undefined") return "";
  const host = window.location.hostname;
  if (host === "localhost" || /^\d+\.\d+\.\d+\.\d+$/.test(host)) return "";
  const parts = host.split(".").filter(Boolean);
  if (parts.length < 2) return "";
  return `; domain=.${parts.slice(-2).join(".")}`;
}

function getCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const prefix = `${name}=`;
  return (
    document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix))
      ?.slice(prefix.length) || ""
  );
}

function writeCookie(name: string, value: AttributionTouch) {
  if (typeof document === "undefined") return;
  try {
    document.cookie = `${name}=${encodeURIComponent(JSON.stringify(value))}; path=/; max-age=31536000; samesite=lax${cookieDomain()}`;
  } catch {
    // Best effort only; localStorage still covers same-origin attribution.
  }
}

export function readAttribution(storageKey: string, cookieName: string): AttributionTouch | null {
  if (typeof window === "undefined") return null;
  const values = [
    (() => {
      try {
        return window.localStorage.getItem(storageKey) || "";
      } catch {
        return "";
      }
    })(),
    (() => {
      try {
        const value = getCookie(cookieName);
        return value ? decodeURIComponent(value) : "";
      } catch {
        return "";
      }
    })(),
  ];

  for (const value of values) {
    if (!value) continue;
    try {
      return JSON.parse(value) as AttributionTouch;
    } catch {
      // Ignore malformed historical values.
    }
  }
  return null;
}

function writeAttribution(storageKey: string, cookieName: string, touch: AttributionTouch) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(touch));
  } catch {
    // Best effort only.
  }
  writeCookie(cookieName, touch);
}

function isExternalReferrer(referrer: string): boolean {
  if (!referrer || typeof window === "undefined") return false;
  try {
    return new URL(referrer).hostname !== window.location.hostname;
  } catch {
    return false;
  }
}

function currentAttributionTouch(): { touch: AttributionTouch; hasInboundSignal: boolean } {
  const params = new URLSearchParams(window.location.search);
  const values = Object.fromEntries(
    TRACKED_QUERY_PARAMS.map((key) => [key, params.get(key) || ""]),
  ) as Record<(typeof TRACKED_QUERY_PARAMS)[number], string>;
  const referrer = document.referrer || "";
  const referrerHost = (() => {
    try {
      return referrer ? new URL(referrer).hostname : "";
    } catch {
      return "";
    }
  })();
  const hasTrackedParam = TRACKED_QUERY_PARAMS.some((key) => Boolean(values[key]));

  return {
    hasInboundSignal: hasTrackedParam || isExternalReferrer(referrer),
    touch: {
      capturedAt: new Date().toISOString(),
      landingUrl: window.location.href,
      landingPath: `${window.location.pathname}${window.location.search}`,
      referrer,
      referrerHost,
      source: values.utm_source,
      medium: values.utm_medium,
      campaign: values.utm_campaign,
      term: values.utm_term,
      content: values.utm_content,
      id: values.utm_id,
      gclid: values.gclid,
      fbclid: values.fbclid,
      msclkid: values.msclkid,
      ttclid: values.ttclid,
      liFatId: values.li_fat_id,
    },
  };
}

export function captureAttribution() {
  if (typeof window === "undefined" || attributionCaptured) return;
  attributionCaptured = true;
  const { touch, hasInboundSignal } = currentAttributionTouch();
  if (!readAttribution(ATTRIBUTION_FIRST_STORAGE_KEY, ATTRIBUTION_COOKIE_FIRST)) {
    writeAttribution(ATTRIBUTION_FIRST_STORAGE_KEY, ATTRIBUTION_COOKIE_FIRST, touch);
  }
  if (hasInboundSignal || !readAttribution(ATTRIBUTION_LATEST_STORAGE_KEY, ATTRIBUTION_COOKIE_LATEST)) {
    writeAttribution(ATTRIBUTION_LATEST_STORAGE_KEY, ATTRIBUTION_COOKIE_LATEST, touch);
  }
}

function attributionTouchProperties(prefix: "first" | "latest", touch: AttributionTouch | null) {
  if (!touch) return {};
  const label = prefix === "first" ? "First" : "Latest";
  return {
    [`a2a${label}CapturedAt`]: touch.capturedAt,
    [`a2a${label}LandingUrl`]: touch.landingUrl,
    [`a2a${label}LandingPath`]: touch.landingPath,
    [`a2a${label}Referrer`]: touch.referrer,
    [`a2a${label}ReferrerHost`]: touch.referrerHost,
    [`a2a${label}UtmSource`]: touch.source,
    [`a2a${label}UtmMedium`]: touch.medium,
    [`a2a${label}UtmCampaign`]: touch.campaign,
    [`a2a${label}UtmTerm`]: touch.term,
    [`a2a${label}UtmContent`]: touch.content,
    [`a2a${label}UtmId`]: touch.id,
    [`a2a${label}Gclid`]: touch.gclid,
    [`a2a${label}Fbclid`]: touch.fbclid,
    [`a2a${label}Msclkid`]: touch.msclkid,
    [`a2a${label}Ttclid`]: touch.ttclid,
    [`a2a${label}LiFatId`]: touch.liFatId,
  };
}

export function getAttributionProperties() {
  captureAttribution();
  const first = readAttribution(ATTRIBUTION_FIRST_STORAGE_KEY, ATTRIBUTION_COOKIE_FIRST);
  const latest = readAttribution(ATTRIBUTION_LATEST_STORAGE_KEY, ATTRIBUTION_COOKIE_LATEST);
  return {
    ...attributionTouchProperties("first", first),
    ...attributionTouchProperties("latest", latest),
  };
}

export function getAnalyticsProperties(options: AttributionOptions): Record<string, unknown> {
  return {
    surface: options.surface,
    environment: options.environment || "production",
    a2aAnonymousId: getAnalyticsAnonymousId(),
    a2aIdentityState: options.identityState || "anonymous",
    ...(options.userId ? { a2aUserId: options.userId } : {}),
    ...getAttributionProperties(),
  };
}
