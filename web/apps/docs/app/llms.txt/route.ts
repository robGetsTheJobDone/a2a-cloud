import { allPages } from "@/lib/content";

// Implements the llms.txt convention (https://llmstxt.org).
//
// First two lines are H1 + summary; the rest is a flat list of
// markdown links to every doc page. Coding agents read this once and
// know what the surface is + where to drill.
export const dynamic = "force-static";

export async function GET() {
  const pages = allPages().sort((a, b) => a.slug.localeCompare(b.slug));
  const lines: string[] = [
    "# a2a",
    "",
    "> Deploy one Python class as a sandboxed, discoverable, billable AI "
      + "agent. Other agents reach yours via Ed25519-signed grants. The "
      + "platform owns deployment, execution, permissions, and the "
      + "marketplace.",
    "",
    "## Docs",
    "",
  ];
  for (const p of pages) {
    const rawPath = p.slug === "" ? "/raw/index.md" : `/raw/${p.slug}.md`;
    lines.push(`- [${p.title}](https://docs.a2acloud.io${rawPath}): ${p.group}`);
  }
  lines.push("");
  lines.push("## Machine-readable");
  lines.push("");
  lines.push(`- [Full corpus (one plaintext blob)](https://docs.a2acloud.io/llms-full.txt)`);
  return new Response(lines.join("\n"), {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
