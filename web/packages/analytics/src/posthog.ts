export type PostHogConfigOptions = {
  apiKey?: string;
  /** PostHog ingest host. Defaults to the first-party reverse proxy. */
  apiUrl?: string;
  scriptUrl?: string;
  surface: string;
  trackOutgoingLinks?: boolean;
  /**
   * Caller IP as seen by the server route that renders this script. Only ever
   * attached to events in full (consent-granted) mode.
   */
  clientIp?: string;
  /** Set false to skip FingerprintJS entirely (e.g. per-surface opt-out). */
  fingerprint?: boolean;
  fingerprintScriptUrl?: string;
};

const DEFAULT_API_URL = "https://e.a2acloud.io";
const DEFAULT_UI_HOST = "https://us.posthog.com";
const DEFAULT_FINGERPRINT_SCRIPT_URL = "https://openfpcdn.io/fingerprintjs/v4";

/**
 * Resolve the caller IP from proxy headers (Traefik sets x-forwarded-for;
 * first hop is the client). Empty string when unknown.
 */
export function clientIpFromHeaders(headers: Headers): string {
  const forwarded = headers.get("x-forwarded-for") || "";
  const first = forwarded.split(",")[0]?.trim();
  return first || headers.get("x-real-ip")?.trim() || "";
}

export function buildPostHogConfigScript(options: PostHogConfigOptions): string {
  const apiKey = options.apiKey?.trim();
  if (!apiKey) return "window.__a2aAnalyticsDisabled=true;\n";

  const apiUrl = (options.apiUrl?.trim() || DEFAULT_API_URL).replace(/\/$/, "");
  const config = JSON.stringify({
    apiKey,
    apiUrl,
    uiHost: DEFAULT_UI_HOST,
    scriptUrl: options.scriptUrl?.trim() || `${apiUrl}/static/array.js`,
    surface: options.surface,
    trackOutgoingLinks: options.trackOutgoingLinks ?? true,
    clientIp: options.clientIp?.trim() || "",
    fingerprint: options.fingerprint ?? true,
    fingerprintScriptUrl:
      options.fingerprintScriptUrl?.trim() || DEFAULT_FINGERPRINT_SCRIPT_URL,
  });

  return `
(function () {
  // Three-state consent gate:
  //   granted -> full mode: persistent anonymous id + stored attribution.
  //   denied  -> nothing. Explicit no is a no.
  //   unset   -> cookieless mode (Plausible model): events flow with an
  //              ephemeral per-pageload id, nothing written to localStorage
  //              or cookies, attribution sent from the current URL only.
  //              Most mobile visitors never touch the banner — without this
  //              the funnel is blind exactly where it matters.
  // Accepting upgrades to full mode on the next navigation (pages are MPA).
  function readConsent() {
    try {
      var found = document.cookie.split(';').map(function (p) { return p.trim(); }).find(function (p) { return p.indexOf('a2a_consent=') === 0; });
      var v = found ? found.slice('a2a_consent='.length) : '';
      return v === 'granted' || v === 'denied' ? v : 'unset';
    } catch (e) { return 'denied'; }
  }
  var consentState = readConsent();
  if (consentState === 'denied') { return; }
  var cookieless = consentState !== 'granted';
  var config = ${config};
  var storageKey = 'a2a.analytics.anonymous_id';
  var firstAttributionKey = 'a2a.analytics.attribution.first';
  var latestAttributionKey = 'a2a.analytics.attribution.latest';
  var firstAttributionCookie = 'a2a_attr_first';
  var latestAttributionCookie = 'a2a_attr_latest';
  var trackedParams = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id', 'gclid', 'fbclid', 'msclkid', 'ttclid', 'li_fat_id'];
  var fallbackAnonymousId = null;
  function randomId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }
  function anonymousId() {
    if (cookieless) {
      // Ephemeral per-pageload id — never persisted, so it is not a device
      // identifier in the consent sense.
      fallbackAnonymousId = fallbackAnonymousId || ('cookieless:' + randomId());
      return fallbackAnonymousId;
    }
    try {
      var existing = window.localStorage.getItem(storageKey);
      if (existing) return existing;
      var next = 'anon:' + randomId();
      window.localStorage.setItem(storageKey, next);
      return next;
    } catch (e) {
      fallbackAnonymousId = fallbackAnonymousId || ('anon:' + randomId());
      return fallbackAnonymousId;
    }
  }
  function cookieDomain() {
    var host = window.location.hostname;
    if (host === 'localhost' || /^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(host)) return '';
    var parts = host.split('.').filter(Boolean);
    return parts.length >= 2 ? '; domain=.' + parts.slice(-2).join('.') : '';
  }
  function getCookie(name) {
    var prefix = name + '=';
    var found = document.cookie.split(';').map(function (part) { return part.trim(); }).find(function (part) { return part.indexOf(prefix) === 0; });
    return found ? found.slice(prefix.length) : '';
  }
  function writeCookie(name, value) {
    try {
      document.cookie = name + '=' + encodeURIComponent(JSON.stringify(value)) + '; path=/; max-age=31536000; samesite=lax' + cookieDomain();
    } catch (e) {}
  }
  function readAttribution(key, cookieName) {
    var values = [];
    try { values.push(window.localStorage.getItem(key) || ''); } catch (e) {}
    try {
      var cookieValue = getCookie(cookieName);
      values.push(cookieValue ? decodeURIComponent(cookieValue) : '');
    } catch (e) {}
    for (var i = 0; i < values.length; i += 1) {
      if (!values[i]) continue;
      try { return JSON.parse(values[i]); } catch (e) {}
    }
    return null;
  }
  function writeAttribution(key, cookieName, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
    writeCookie(cookieName, value);
  }
  function externalReferrer(referrer) {
    if (!referrer) return false;
    try { return new URL(referrer).hostname !== window.location.hostname; } catch (e) { return false; }
  }
  function currentTouch() {
    var params = new URLSearchParams(window.location.search);
    var value = function (key) { return params.get(key) || ''; };
    var referrer = document.referrer || '';
    var hasTrackedParam = trackedParams.some(function (key) { return !!value(key); });
    var referrerHost = '';
    try { referrerHost = referrer ? new URL(referrer).hostname : ''; } catch (e) {}
    return {
      hasInboundSignal: hasTrackedParam || externalReferrer(referrer),
      touch: {
        capturedAt: new Date().toISOString(),
        landingUrl: window.location.href,
        landingPath: window.location.pathname + window.location.search,
        referrer: referrer,
        referrerHost: referrerHost,
        source: value('utm_source'),
        medium: value('utm_medium'),
        campaign: value('utm_campaign'),
        term: value('utm_term'),
        content: value('utm_content'),
        id: value('utm_id'),
        gclid: value('gclid'),
        fbclid: value('fbclid'),
        msclkid: value('msclkid'),
        ttclid: value('ttclid'),
        liFatId: value('li_fat_id')
      }
    };
  }
  function captureAttribution() {
    var current = currentTouch();
    if (!readAttribution(firstAttributionKey, firstAttributionCookie)) {
      writeAttribution(firstAttributionKey, firstAttributionCookie, current.touch);
    }
    if (current.hasInboundSignal || !readAttribution(latestAttributionKey, latestAttributionCookie)) {
      writeAttribution(latestAttributionKey, latestAttributionCookie, current.touch);
    }
  }
  function touchProperties(prefix, touch) {
    if (!touch) return {};
    var label = prefix === 'first' ? 'First' : 'Latest';
    var out = {};
    out['a2a' + label + 'CapturedAt'] = touch.capturedAt;
    out['a2a' + label + 'LandingUrl'] = touch.landingUrl;
    out['a2a' + label + 'LandingPath'] = touch.landingPath;
    out['a2a' + label + 'Referrer'] = touch.referrer;
    out['a2a' + label + 'ReferrerHost'] = touch.referrerHost;
    out['a2a' + label + 'UtmSource'] = touch.source;
    out['a2a' + label + 'UtmMedium'] = touch.medium;
    out['a2a' + label + 'UtmCampaign'] = touch.campaign;
    out['a2a' + label + 'UtmTerm'] = touch.term;
    out['a2a' + label + 'UtmContent'] = touch.content;
    out['a2a' + label + 'UtmId'] = touch.id;
    out['a2a' + label + 'Gclid'] = touch.gclid;
    out['a2a' + label + 'Fbclid'] = touch.fbclid;
    out['a2a' + label + 'Msclkid'] = touch.msclkid;
    out['a2a' + label + 'Ttclid'] = touch.ttclid;
    out['a2a' + label + 'LiFatId'] = touch.liFatId;
    return out;
  }
  function attributionState() {
    if (cookieless) {
      // Nothing read from or written to storage — report the current touch
      // (UTM params, referrer of this very pageload) as the latest one.
      return touchProperties('latest', currentTouch().touch);
    }
    captureAttribution();
    return Object.assign(
      {},
      touchProperties('first', readAttribution(firstAttributionKey, firstAttributionCookie)),
      touchProperties('latest', readAttribution(latestAttributionKey, latestAttributionCookie))
    );
  }
  // Device signals live only in full mode. Cookieless visitors must stay
  // ephemeral: no fingerprint, no IP property — that is the consent contract.
  // The fp id is mirrored into a registrable-domain cookie so (a) later
  // pageloads carry it from the first event without waiting on the fp lib,
  // and (b) the dashboard can attach it to the signed-up user profile.
  // Kept as a person property (not only an alias) so the marketing fp person
  // and the product user person stay joinable even across resets.
  var fpCookie = 'a2a_fp';
  var fingerprintId = null;
  if (!cookieless) {
    try { fingerprintId = decodeURIComponent(getCookie(fpCookie)) || null; } catch (e) {}
  }
  function deviceSignals() {
    if (cookieless) return {};
    var out = {};
    if (fingerprintId) out.a2aFingerprintId = fingerprintId;
    if (config.clientIp) out.a2aClientIp = config.clientIp;
    return out;
  }
  function attribution() {
    return Object.assign({
      surface: config.surface,
      environment: 'production',
      a2aAnonymousId: anonymousId(),
      a2aIdentityState: cookieless ? 'cookieless' : 'anonymous'
    }, deviceSignals(), attributionState());
  }
  // Official PostHog queue stub: calls made before array.js loads are queued
  // and replayed. Script src is forced to config.scriptUrl (first-party proxy).
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=config.scriptUrl,(r=t.getElementsByTagName("script")[0])&&r.parentNode?r.parentNode.insertBefore(p,r):t.head.appendChild(p);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
  window.posthog.init(config.apiKey, {
    api_host: config.apiUrl,
    ui_host: config.uiHost,
    defaults: '2025-05-24',
    // Custom click handler below keeps event parity with the old pipeline;
    // autocapture would double-count every link click.
    autocapture: false,
    capture_pageview: true,
    capture_pageleave: true,
    disable_session_recording: true,
    disable_surveys: true,
    // Cookieless visitors must leave no trace: memory persistence means the
    // distinct id and super properties die with the pageload.
    persistence: cookieless ? 'memory' : 'localStorage+cookie',
    cross_subdomain_cookie: true
  });
  window.posthog.register(attribution());
  function identifyFingerprint() {
    window.posthog.identify('fp:' + fingerprintId, Object.assign({ fingerprintId: fingerprintId }, attribution()));
  }
  // Cookie from an earlier visit makes the fp identity available from the
  // very first event of this pageload, before the fp lib resolves.
  if (fingerprintId) identifyFingerprint();
  // FingerprintJS runs only in full mode, after the queue stub exists. Init is
  // not delayed on it: the first pageview goes out fingerprint-less (unless
  // the cookie seeded it above), then the identify below merges that same
  // device's events into the fp person, so nothing is lost and page load
  // pays zero fingerprint latency.
  if (!cookieless && config.fingerprint && config.fingerprintScriptUrl) {
    try {
      import(/* webpackIgnore: true */ config.fingerprintScriptUrl)
        .then(function (mod) { return (mod && mod.default ? mod.default : mod).load(); })
        .then(function (agent) { return agent.get(); })
        .then(function (result) {
          if (!result || !result.visitorId || !window.posthog) return;
          if (readConsent() !== 'granted') return; // declined since pageload
          try {
            document.cookie = fpCookie + '=' + encodeURIComponent(result.visitorId) + '; path=/; max-age=31536000; samesite=lax' + cookieDomain();
          } catch (e) {}
          if (result.visitorId === fingerprintId) return; // cookie already did the work
          fingerprintId = result.visitorId;
          window.posthog.register(attribution());
          identifyFingerprint();
        })
        .catch(function () { /* fingerprint is best-effort */ });
    } catch (e) { /* dynamic import unsupported — skip */ }
  }
  document.addEventListener('click', function (event) {
    var target = event.target && event.target.closest ? event.target.closest('[data-analytics-event],a[href]') : null;
    if (!target || !window.posthog) return;
    // Re-check at click time: covers "Decline" pressed after this pageload.
    if (readConsent() === 'denied') return;
    var eventName = target.getAttribute('data-analytics-event') || 'link_click';
    var href = target.getAttribute('href') || '';
    window.posthog.capture(eventName, Object.assign(attribution(), {
      surface: config.surface,
      href: href,
      path: window.location.pathname,
      label: (target.textContent || '').trim().slice(0, 80)
    }));
  }, true);
})();
`;
}
