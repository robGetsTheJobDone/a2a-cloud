#!/bin/sh
set -eu

# Analytics are off unless both POSTHOG_KEY and POSTHOG_API_URL (your PostHog
# ingest host or reverse proxy) are configured.
api_key="${POSTHOG_KEY:-}"
api_url="${POSTHOG_API_URL:-}"
script_url="${POSTHOG_SCRIPT_URL:-${api_url}/static/array.js}"
out="/usr/share/nginx/html/analytics-config.js"

if [ -z "$api_key" ] || [ -z "$api_url" ]; then
  printf '%s\n' 'window.__a2aAnalyticsDisabled = true;' > "$out"
  exit 0
fi

cat > "$out" <<EOF
(function () {
  var config = {
    apiKey: "$api_key",
    apiUrl: "$api_url",
    scriptUrl: "$script_url",
    surface: "app"
  };
  var storageKey = "a2a.analytics.anonymous_id";
  var firstAttributionKey = "a2a.analytics.attribution.first";
  var latestAttributionKey = "a2a.analytics.attribution.latest";
  var firstAttributionCookie = "a2a_attr_first";
  var latestAttributionCookie = "a2a_attr_latest";
  var trackedParams = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "gclid", "fbclid", "msclkid", "ttclid", "li_fat_id"];
  var fallbackAnonymousId = null;
  function randomId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
  }
  function anonymousId() {
    try {
      var existing = window.localStorage.getItem(storageKey);
      if (existing) return existing;
      var next = "anon:" + randomId();
      window.localStorage.setItem(storageKey, next);
      return next;
    } catch (e) {
      fallbackAnonymousId = fallbackAnonymousId || ("anon:" + randomId());
      return fallbackAnonymousId;
    }
  }
  function cookieDomain() {
    var host = window.location.hostname;
    if (host === "localhost" || /^\\d+\\.\\d+\\.\\d+\\.\\d+$/.test(host)) return "";
    var parts = host.split(".").filter(Boolean);
    return parts.length >= 2 ? "; domain=." + parts.slice(-2).join(".") : "";
  }
  function getCookie(name) {
    var prefix = name + "=";
    var found = document.cookie.split(";").map(function (part) { return part.trim(); }).find(function (part) { return part.indexOf(prefix) === 0; });
    return found ? found.slice(prefix.length) : "";
  }
  function writeCookie(name, value) {
    try {
      document.cookie = name + "=" + encodeURIComponent(JSON.stringify(value)) + "; path=/; max-age=31536000; samesite=lax" + cookieDomain();
    } catch (e) {}
  }
  function readAttribution(key, cookieName) {
    var values = [];
    try { values.push(window.localStorage.getItem(key) || ""); } catch (e) {}
    try {
      var cookieValue = getCookie(cookieName);
      values.push(cookieValue ? decodeURIComponent(cookieValue) : "");
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
    var value = function (key) { return params.get(key) || ""; };
    var referrer = document.referrer || "";
    var hasTrackedParam = trackedParams.some(function (key) { return !!value(key); });
    var referrerHost = "";
    try { referrerHost = referrer ? new URL(referrer).hostname : ""; } catch (e) {}
    return {
      hasInboundSignal: hasTrackedParam || externalReferrer(referrer),
      touch: {
        capturedAt: new Date().toISOString(),
        landingUrl: window.location.href,
        landingPath: window.location.pathname + window.location.search,
        referrer: referrer,
        referrerHost: referrerHost,
        source: value("utm_source"),
        medium: value("utm_medium"),
        campaign: value("utm_campaign"),
        term: value("utm_term"),
        content: value("utm_content"),
        id: value("utm_id"),
        gclid: value("gclid"),
        fbclid: value("fbclid"),
        msclkid: value("msclkid"),
        ttclid: value("ttclid"),
        liFatId: value("li_fat_id")
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
    var label = prefix === "first" ? "First" : "Latest";
    var out = {};
    out["a2a" + label + "CapturedAt"] = touch.capturedAt;
    out["a2a" + label + "LandingUrl"] = touch.landingUrl;
    out["a2a" + label + "LandingPath"] = touch.landingPath;
    out["a2a" + label + "Referrer"] = touch.referrer;
    out["a2a" + label + "ReferrerHost"] = touch.referrerHost;
    out["a2a" + label + "UtmSource"] = touch.source;
    out["a2a" + label + "UtmMedium"] = touch.medium;
    out["a2a" + label + "UtmCampaign"] = touch.campaign;
    out["a2a" + label + "UtmTerm"] = touch.term;
    out["a2a" + label + "UtmContent"] = touch.content;
    out["a2a" + label + "UtmId"] = touch.id;
    out["a2a" + label + "Gclid"] = touch.gclid;
    out["a2a" + label + "Fbclid"] = touch.fbclid;
    out["a2a" + label + "Msclkid"] = touch.msclkid;
    out["a2a" + label + "Ttclid"] = touch.ttclid;
    out["a2a" + label + "LiFatId"] = touch.liFatId;
    return out;
  }
  function attributionState() {
    captureAttribution();
    return Object.assign(
      {},
      touchProperties("first", readAttribution(firstAttributionKey, firstAttributionCookie)),
      touchProperties("latest", readAttribution(latestAttributionKey, latestAttributionCookie))
    );
  }
  function attribution() {
    return Object.assign({
      surface: config.surface,
      environment: "production",
      a2aAnonymousId: anonymousId(),
      a2aIdentityState: "anonymous"
    }, attributionState());
  }
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=config.scriptUrl,(r=t.getElementsByTagName("script")[0])&&r.parentNode?r.parentNode.insertBefore(p,r):t.head.appendChild(p);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
  window.posthog.init(config.apiKey, {
    api_host: config.apiUrl,
    ui_host: "https://us.posthog.com",
    defaults: "2025-05-24",
    autocapture: false,
    capture_pageview: true,
    capture_pageleave: true,
    disable_session_recording: true,
    disable_surveys: true,
    persistence: "localStorage+cookie",
    cross_subdomain_cookie: true
  });
  window.posthog.register(attribution());
})();
EOF
