// Safe serializer for JSON-LD injected via `dangerouslySetInnerHTML`.
//
// WHY: `JSON.stringify` escapes what JSON needs, but it does NOT escape "<".
// Inside a `<script type="application/ld+json">` block the browser's HTML
// tokenizer, not the JSON parser, decides where the element ends, so a string
// carrying `</script><img src=x onerror=alert(1)>` closes the script early and
// the remainder is parsed as markup and executed. Any JSON-LD field that can
// hold user-authored text (agent cards, page copy,
// docs headings and markdown-derived descriptions) is a stored-XSS vector
// without this.
//
// The replacements are \uXXXX JSON string escapes, which `JSON.parse` decodes
// back to the original characters, so consumers (Google included) still read
// exactly the same structured data.
//
// U+2028/U+2029 are legal raw inside JSON strings but are line terminators in
// JavaScript source; escaping them keeps the payload safe for anything that
// evaluates rather than parses it.
//
// Lives in the docs app rather than a workspace package: the app tsconfig
// `@/*` alias is app-scoped, and `node --test` refuses to strip types inside
// node_modules, so a shared package would be unimportable from the tests.
const SCRIPT_UNSAFE = /[<>&\u2028\u2029]/g;

const SCRIPT_ESCAPES: Record<string, string> = {
  "<": "\\u003c",
  ">": "\\u003e",
  "&": "\\u0026",
  "\u2028": "\\u2028",
  "\u2029": "\\u2029",
};

/** Serialize `value` for use as the `__html` of a JSON-LD `<script>` tag. */
export function jsonLdScript(value: unknown): string {
  const json = JSON.stringify(value) ?? "null";
  return json.replace(SCRIPT_UNSAFE, (char) => SCRIPT_ESCAPES[char]);
}
