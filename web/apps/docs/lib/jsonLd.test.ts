import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import test from "node:test";

// Node's native type-stripping runner requires the explicit extension.
// @ts-expect-error TypeScript's bundler mode omits it for application imports.
import { jsonLdScript } from "./jsonLd.ts";

const BREAKOUT = '</script><img src=x onerror=alert(1)>';

// Mirrors the @graph the docs page builds: `headline` comes from a markdown H1,
// `description` from the first paragraph of the page body, and the breadcrumb
// names from the URL slug. All three are content-derived, so a heading
// containing markup would previously have escaped the <script> block verbatim.
function docsJsonLd(overrides: Record<string, unknown> = {}) {
  const canonical = "https://docs.a2acloud.io/reference/cli";
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "TechArticle",
        "@id": `${canonical}#article`,
        url: canonical,
        headline: `CLI reference ${BREAKOUT}`,
        description: `Deploy & govern agents. ${BREAKOUT} 5 > 3`,
        dateModified: "2026-08-01T00:00:00.000Z",
        inLanguage: "en",
        ...overrides,
      },
      {
        "@type": "BreadcrumbList",
        "@id": `${canonical}#breadcrumb`,
        itemListElement: [
          { "@type": "ListItem", position: 1, name: "a2a cloud docs", item: "https://docs.a2acloud.io/" },
          { "@type": "ListItem", position: 2, name: `reference ${BREAKOUT}`, item: canonical },
        ],
      },
    ],
  };
}

test("a script-breakout payload cannot terminate the script element", () => {
  const html = jsonLdScript(docsJsonLd());

  assert.equal(html.includes("</script>"), false);
  assert.equal(html.includes("<"), false);
  assert.equal(html.includes(">"), false);
  assert.equal(html.includes("&"), false);
  assert.ok(html.includes("\\u003c/script\\u003e"));
});

test("escaped output still parses back to the original object", () => {
  const value = docsJsonLd();

  // schema.org validity: a broken escape would silently destroy the structured
  // data instead of failing loudly, so assert the exact round trip.
  assert.deepEqual(JSON.parse(jsonLdScript(value)), value);
});

test("U+2028 and U+2029 are escaped but survive the round trip", () => {
  const value = docsJsonLd({ description: "line\u2028break\u2029here" });
  const html = jsonLdScript(value);

  assert.equal(html.includes("\u2028"), false);
  assert.equal(html.includes("\u2029"), false);
  assert.ok(html.includes("\\u2028"));
  assert.deepEqual(JSON.parse(html), value);
});

test("undefined serializes to valid JSON instead of throwing", () => {
  assert.equal(jsonLdScript(undefined), "null");
});

// --- repo invariants -------------------------------------------------------
// A correct helper is worthless if a new page reaches for `JSON.stringify`
// again, so this app's call sites are checked as data.

const APP_ROOT = path.resolve(import.meta.dirname, "..");
const APPS_ROOT = path.resolve(APP_ROOT, "..");

const SCANNED_APPS = [APP_ROOT];

// The `__html` expression that follows a `type="application/ld+json"` attribute.
const LD_JSON_HTML = /application\/ld\+json[\s\S]{0,400}?__html:\s*([^\n]+)/g;
const LD_JSON = /application\/ld\+json/g;

// Every raw-HTML sink, whichever way it is spelled: the JSX form
// (`dangerouslySetInnerHTML={{ … }}`) and the createElement form
// (`dangerouslySetInnerHTML: { … }`). Anchoring on the sink instead of on the
// mime literal is what makes this guard survive a JSON-LD site that never
// writes "application/ld+json" itself — a shared <JsonLd> component, or
// `type={LD_MIME}`, still has to reach a sink to render anything.
const SINK = /dangerouslySetInnerHTML\s*[=:]\s*\{/g;
const SINK_HTML = /dangerouslySetInnerHTML\s*[=:]\s*\{\s*\{?\s*__html:\s*([^\n]*)/g;

// Raw-HTML sinks that are deliberately not JSON-LD, keyed by their path under
// web/apps and mapped to the exact `__html` expression they may pass. Kept
// identical to the landing copy; entries outside this app's scanned roots are
// ignored. Adding a sink means adding it here on purpose.
const ALLOWED_RAW_HTML: Record<string, string> = {
  // marked output for the markdown under apps/docs/content, which is
  // maintainer-authored or generated from repo sources by apps/docs/scripts.
  // marked >= 5 dropped its `sanitize` option, so raw HTML in a docs page
  // renders verbatim: if that content ever takes non-maintainer input, this
  // needs a sanitizer between marked and the sink.
  "docs/components/Markdown.tsx": "html",
};

const SKIPPED_DIRS = new Set(["node_modules", ".next", ".turbo", "dist", "coverage", "out"]);

// `isDirectory()` is false for a symlinked directory, so a symlinked tree would
// be skipped — that fails closed, because the counts asserted below drop below
// their floor rather than silently passing.
function sources(root: string): string[] {
  const files: string[] = [];

  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      if (!SKIPPED_DIRS.has(entry.name)) files.push(...sources(full));
      // Test files are excluded: they quote these very regexes as data.
    } else if (/\.tsx?$/.test(entry.name) && !/(\.test|\.d)\.tsx?$/.test(entry.name)) {
      files.push(full);
    }
  }

  return files;
}

function normalizeHtmlExpr(raw: string): string {
  return raw.replace(/[\s,}]+$/, "").trim();
}

test("every JSON-LD script in apps/docs is serialized by jsonLdScript", () => {
  const offenders: string[] = [];
  let matched = 0;
  let declared = 0;

  // .tsx only: lib/jsonLd.ts names the mime type in a comment, which would
  // count as a declaration with no sink to pair it with. A JSON-LD site in a
  // .ts file is still caught by the sink scan below.
  for (const file of sources(APP_ROOT).filter((f) => f.endsWith(".tsx"))) {
    const source = readFileSync(file, "utf8");
    declared += source.match(LD_JSON)?.length ?? 0;
    for (const match of source.matchAll(LD_JSON_HTML)) {
      matched += 1;
      if (!match[1].trimStart().startsWith("jsonLdScript(")) {
        offenders.push(`${path.relative(APP_ROOT, file)}: ${match[1].trim()}`);
      }
    }
  }

  assert.deepEqual(offenders, []);
  // Guards the scan itself: without these a regex that stopped matching would
  // pass silently on zero files.
  assert.equal(matched, declared, "a JSON-LD script tag was not matched by the scan");
  assert.ok(matched >= 1, `expected to scan the docs page JSON-LD site, saw ${matched}`);
});

test("every raw-HTML sink in apps/docs is jsonLdScript or explicitly allowed", () => {
  const offenders: string[] = [];
  const usedAllowances = new Set<string>();
  let sinks = 0;
  let matched = 0;

  for (const root of SCANNED_APPS) {
    for (const file of sources(root)) {
      const source = readFileSync(file, "utf8");
      const key = path.relative(APPS_ROOT, file);
      sinks += source.match(SINK)?.length ?? 0;
      for (const match of source.matchAll(SINK_HTML)) {
        matched += 1;
        const expr = normalizeHtmlExpr(match[1]);
        if (expr.startsWith("jsonLdScript(")) continue;
        if (ALLOWED_RAW_HTML[key] === expr) {
          usedAllowances.add(key);
          continue;
        }
        offenders.push(`${key}: ${expr}`);
      }
    }
  }

  assert.deepEqual(offenders, []);
  assert.equal(matched, sinks, "a dangerouslySetInnerHTML sink was not matched by the scan");
  assert.ok(matched >= 2, `expected to scan the known raw-HTML sinks, saw ${matched}`);
  // Keeps the allowlist from rotting into a rubber stamp for a file that has
  // since moved or stopped being a sink. Only entries under a scanned root are
  // expected, so this map can stay identical to the landing copy.
  const expected = Object.keys(ALLOWED_RAW_HTML).filter((key) =>
    SCANNED_APPS.some((root) => path.resolve(APPS_ROOT, key).startsWith(`${root}${path.sep}`)),
  );
  assert.deepEqual(
    [...usedAllowances].sort(),
    expected.sort(),
    "an allowlisted raw-HTML sink no longer exists",
  );
});
