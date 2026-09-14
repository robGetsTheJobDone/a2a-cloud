import fs from "node:fs";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Link from "next/link";
import { Markdown } from "@/components/Markdown";
import { Sidebar, MobileNav } from "@/components/Sidebar";
import { allPages, getPage } from "@/lib/content";
import { jsonLdScript } from "@/lib/jsonLd";

const SITE_URL = "https://docs.a2acloud.io";

const SEO_OVERRIDES: Record<string, { title: string; description: string }> = {
  "reference/deepagents": {
    title: "a2a_pack.deepagents API Reference | a2a cloud docs",
    description:
      "API reference for a2a_pack.deepagents, including its workspace-backed DeepAgents backend, sandbox integration, and runtime helpers.",
  },
};

function pageTitle(flat: string, fallbackTitle: string): string {
  return SEO_OVERRIDES[flat]?.title ?? (flat === ""
    ? "a2a cloud Documentation — deploy, run & govern AI agents"
    : `${fallbackTitle} | a2a cloud docs`);
}

function pageDescription(flat: string, fallbackTitle: string, markdown: string): string {
  return SEO_OVERRIDES[flat]?.description ?? (flat === ""
    ? "Official a2a cloud documentation. Deploy Python or TypeScript agents as hosted A2A + MCP services — quickstart, CLI, runtime, grants, auth, and sandbox reference."
    : descFromMarkdown(markdown) ||
      `${fallbackTitle} — a2a cloud documentation for deploying and governing AI agents.`);
}

export async function generateStaticParams() {
  return allPages().map((p) => ({
    slug: p.slug === "" ? [] : p.slug.split("/"),
  }));
}

type Props = { params: Promise<{ slug?: string[] }> };

// Pull the first real paragraph out of the markdown for the meta description.
// Skips the H1, code fences, tables, and other headings.
function descFromMarkdown(md: string): string {
  const body = md.replace(/```[\s\S]*?```/g, "");
  let para = "";
  for (const raw of body.split("\n")) {
    const t = raw.trim();
    if (!t) {
      if (para) break;
      continue;
    }
    if (t.startsWith("#") || t.startsWith("|") || t.startsWith("-") || t.startsWith("```")) {
      if (para) break;
      continue;
    }
    para += (para ? " " : "") + t;
    if (para.length > 180) break;
  }
  para = para
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[`*_>]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (para.length > 155) para = para.slice(0, 152).replace(/\s+\S*$/, "") + "…";
  return para;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const flat = (slug || []).join("/");
  const page = getPage(flat);
  if (!page) return { title: "Not found | a2a cloud docs" };

  const title = pageTitle(flat, page.title);
  const description = pageDescription(flat, page.title, page.markdown);
  const canonical = `${SITE_URL}/${flat}`;

  return {
    title,
    description,
    alternates: { canonical },
    openGraph: {
      title,
      description,
      url: canonical,
      siteName: "a2a cloud docs",
      type: "article",
    },
    twitter: { card: "summary_large_image", title, description },
    robots: { index: true, follow: true },
  };
}

export default async function Page({ params }: Props) {
  const { slug } = await params;
  const flat = (slug || []).join("/");
  const page = getPage(flat);
  if (!page) return notFound();
  const canonical = `${SITE_URL}/${flat}`;
  const title = pageTitle(flat, page.title);
  const description = pageDescription(flat, page.title, page.markdown);
  const modified = fs.statSync(page.filepath).mtime.toISOString();
  const breadcrumbItems = [
    {
      "@type": "ListItem",
      position: 1,
      name: "a2a cloud docs",
      item: `${SITE_URL}/`,
    },
    ...flat.split("/").filter(Boolean).map((part, index, parts) => ({
      "@type": "ListItem",
      position: index + 2,
      name: part.replace(/-/g, " "),
      item: `${SITE_URL}/${parts.slice(0, index + 1).join("/")}`,
    })),
  ];
  const jsonLd = {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "TechArticle",
        "@id": `${canonical}#article`,
        url: canonical,
        headline: title,
        description,
        dateModified: modified,
        inLanguage: "en",
        mainEntityOfPage: { "@id": canonical },
        author: { "@type": "Organization", name: "a2a cloud", url: "https://a2acloud.io" },
        publisher: { "@type": "Organization", name: "a2a cloud", url: "https://a2acloud.io" },
      },
      {
        "@type": "BreadcrumbList",
        "@id": `${canonical}#breadcrumb`,
        itemListElement: breadcrumbItems,
      },
    ],
  };
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: jsonLdScript(jsonLd) }}
      />
      <MobileNav activeSlug={page.slug} />
      <div className="mx-auto flex max-w-7xl">
        <Sidebar activeSlug={page.slug} />
      <main className="min-w-0 flex-1 px-6 py-10 md:px-12">
        <div className="mx-auto max-w-3xl">
          <div className="mb-6 flex items-center gap-2 text-xs text-neutral-500">
            <Link href="/" className="hover:text-neutral-200">a2a docs</Link>
            {page.slug && (
              <>
                <span>›</span>
                <span className="capitalize">{page.group}</span>
                {page.slug.split("/").slice(1).map((part, i) => (
                  <span key={i} className="flex items-center gap-2">
                    <span>›</span><span>{part.replace(/-/g, " ")}</span>
                  </span>
                ))}
              </>
            )}
          </div>
          <Markdown source={page.markdown} />
          <footer className="mt-16 border-t border-neutral-900 pt-6 text-xs text-neutral-500">
            <a href="/llms.txt" className="mr-4 hover:text-neutral-200">/llms.txt</a>
            <a href="/llms-full.txt" className="hover:text-neutral-200">/llms-full.txt</a>
          </footer>
        </div>
        </main>
      </div>
    </>
  );
}
