"use client";

import { useEffect, useRef, useState } from "react";

// Shared consent contract across every *.a2acloud.io surface. The cookie is set
// on the registrable domain so a decision on one site (landing/docs)
// carries to the others. The same cookie name is read by the PostHog loader
// (@a2a/analytics posthog.ts) as a second, independent gate.
const CONSENT_COOKIE = "a2a_consent";
const CONSENT_EVENT = "a2a:open-consent";
const ANALYTICS_SRC = "/analytics-config.js";
const MAX_AGE = 60 * 60 * 24 * 182; // ~6 months

type Decision = "granted" | "denied";

function cookieDomain(): string {
  if (typeof window === "undefined") return "";
  const host = window.location.hostname;
  if (host === "localhost" || /^\d+\.\d+\.\d+\.\d+$/.test(host)) return "";
  const parts = host.split(".").filter(Boolean);
  if (parts.length < 2) return "";
  return `; domain=.${parts.slice(-2).join(".")}`;
}

function readConsent(): Decision | null {
  if (typeof document === "undefined") return null;
  const prefix = `${CONSENT_COOKIE}=`;
  const found = document.cookie
    .split(";")
    .map((p) => p.trim())
    .find((p) => p.startsWith(prefix));
  const value = found ? found.slice(prefix.length) : "";
  return value === "granted" || value === "denied" ? value : null;
}

function writeConsent(value: Decision) {
  try {
    document.cookie = `${CONSENT_COOKIE}=${value}; path=/; max-age=${MAX_AGE}; samesite=lax${cookieDomain()}`;
  } catch {
    /* best effort */
  }
}

// Consent withdrawal must also erase what full mode stored: fingerprint id,
// persistent anonymous id, attribution touches. Names mirror @a2a/analytics.
function clearTrackingState() {
  try {
    for (const name of ["a2a_fp", "a2a_attr_first", "a2a_attr_latest"]) {
      document.cookie = `${name}=; path=/; max-age=0${cookieDomain()}`;
    }
  } catch {
    /* best effort */
  }
  try {
    window.localStorage.removeItem("a2a.analytics.anonymous_id");
    window.localStorage.removeItem("a2a.analytics.attribution.first");
    window.localStorage.removeItem("a2a.analytics.attribution.latest");
  } catch {
    /* best effort */
  }
}

/** Re-open the consent chooser (e.g. from a "Cookie preferences" link). */
export function openConsentPreferences() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(CONSENT_EVENT));
}

export function ConsentBanner({
  policyHref = "https://a2acloud.io/privacy",
}: {
  policyHref?: string;
}) {
  const [open, setOpen] = useState(false);
  const injected = useRef(false);

  function injectAnalytics() {
    if (injected.current) return;
    injected.current = true;
    if (document.querySelector("script[data-a2a-analytics]")) return;
    const script = document.createElement("script");
    script.src = ANALYTICS_SRC;
    script.async = true;
    script.setAttribute("data-a2a-analytics", "");
    document.body.appendChild(script);
  }

  useEffect(() => {
    const decision = readConsent();
    // No decision yet still injects: the loader self-detects the missing
    // cookie and runs in cookieless mode (ephemeral id, no stored
    // attribution — see @a2a/analytics posthog.ts). Only an explicit
    // "denied" keeps analytics out entirely.
    if (decision !== "denied") injectAnalytics();
    if (decision === null) setOpen(true);
    const reopen = () => setOpen(true);
    window.addEventListener(CONSENT_EVENT, reopen);
    return () => window.removeEventListener(CONSENT_EVENT, reopen);
  }, []);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-label="Cookie consent"
      aria-live="polite"
      className="fixed inset-x-0 top-[4.25rem] z-[100] px-3 sm:bottom-0 sm:top-auto sm:px-4 sm:pb-4"
    >
      {/* No backdrop-blur: the surface is already ~opaque and blurring the
          animated hero video behind it made accept-taps lag on phones. */}
      <div className="mx-auto flex max-w-3xl items-center gap-2 rounded-runtime-md border border-runtime-mint-line bg-runtime-field/95 p-2 shadow-hard-mint sm:justify-between sm:gap-6 sm:rounded-runtime-lg sm:p-4">
        <p className="min-w-0 flex-1 text-[10px] leading-4 text-ink-dim sm:text-sm sm:leading-relaxed">
          <span className="sm:hidden">
            Optional analytics. Decline stays cookieless.{" "}
          </span>
          <span className="hidden sm:inline">
            We use cookies for privacy-friendly analytics — including anonymized
            usage and session insights — to improve the product. If you accept, we
            also use a device identifier (browser fingerprint) and your IP address
            to recognize returning visits. Essential cookies always stay on.{" "}
          </span>
          <a
            href={policyHref}
            className="text-brand-volt underline underline-offset-2 hover:text-ink"
          >
            Privacy &amp; cookies
          </a>
        </p>
        {/* Full-width, taller buttons on phones — thumb targets; compact
            inline pair from sm: up. */}
        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          <button
            type="button"
            onClick={() => {
              writeConsent("denied");
              clearTrackingState();
              setOpen(false);
            }}
            className="touch-manipulation rounded-runtime-md border border-runtime-mint-line px-2.5 py-2 font-mono text-[10px] text-ink-dim hover:text-ink sm:px-3 sm:py-1.5 sm:text-xs"
          >
            Decline
          </button>
          <button
            type="button"
            onClick={() => {
              writeConsent("granted");
              injectAnalytics();
              setOpen(false);
            }}
            className="touch-manipulation rounded-runtime-md border border-brand-volt bg-brand-volt px-2.5 py-2 font-mono text-[10px] font-medium text-runtime-bg hover:bg-brand-field-hover sm:px-3 sm:py-1.5 sm:text-xs"
          >
            Accept
          </button>
        </div>
      </div>
    </div>
  );
}
