import { getPage } from "@/lib/content";

export const dynamic = "force-static";

type RouteContext = {
  params: Promise<{ slug?: string[] }>;
};

function contentSlug(parts: string[] | undefined): string {
  const joined = (parts || []).join("/");
  const withoutExtension = joined.endsWith(".md") ? joined.slice(0, -3) : joined;
  return withoutExtension === "index" ? "" : withoutExtension;
}

export async function GET(_request: Request, { params }: RouteContext) {
  const { slug } = await params;
  const page = getPage(contentSlug(slug));
  if (!page) {
    return new Response("Not found\n", {
      status: 404,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  return new Response(page.markdown, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=300, s-maxage=3600",
    },
  });
}
