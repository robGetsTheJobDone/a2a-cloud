import { allMarkdownConcatenated } from "@/lib/content";

// Full-corpus plain-text dump. Coding agents that don't want to chase
// multiple page fetches can pull this once and get every doc page
// concatenated with `# Page: <slug>` headers between them.
export const dynamic = "force-static";

export async function GET() {
  const body = allMarkdownConcatenated();
  return new Response(body, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
