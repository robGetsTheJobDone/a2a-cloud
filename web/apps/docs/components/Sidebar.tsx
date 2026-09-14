import Link from "next/link";
import { navTree, type PageRef } from "@/lib/content";

const GROUP_ORDER = ["home", "platform", "concepts", "languages", "reference", "examples"];
const GROUP_TITLES: Record<string, string> = {
  home: "Start here",
  platform: "Platform",
  concepts: "Concepts",
  languages: "Languages",
  reference: "Reference",
  examples: "Examples",
};

function orderedGroups(tree: Record<string, PageRef[]>): string[] {
  return [
    ...GROUP_ORDER.filter((g) => tree[g]),
    ...Object.keys(tree).filter((g) => !GROUP_ORDER.includes(g)),
  ];
}

function NavGroups({ activeSlug }: { activeSlug: string }) {
  const tree = navTree();
  return (
    <>
      {orderedGroups(tree).map((g) => (
        <div key={g} className="mb-6">
          <div className="mb-2 text-xs uppercase tracking-wider text-neutral-500">
            {GROUP_TITLES[g] || g}
          </div>
          <ul className="space-y-1">
            {tree[g].map((p) => {
              const href = p.slug === "" ? "/" : `/${p.slug}`;
              const active = activeSlug === p.slug;
              return (
                <li key={p.slug}>
                  <Link
                    href={href}
                    className={
                      "flex items-center justify-between gap-2 rounded px-2 py-1 text-sm transition " +
                      (active
                        ? "bg-violet-500/10 text-violet-200"
                        : "text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100")
                    }
                  >
                    <span className="min-w-0 truncate">{p.title}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
  );
}

function LlmLinks() {
  return (
    <div className="mt-10 border-t border-neutral-900 pt-4 text-xs text-neutral-500">
      <div className="mb-1 uppercase tracking-wider">For LLMs</div>
      <a href="/llms.txt" className="block hover:text-neutral-200">/llms.txt</a>
      <a href="/llms-full.txt" className="block hover:text-neutral-200">/llms-full.txt</a>
    </div>
  );
}

// Desktop: sticky left rail. Hidden below md.
export function Sidebar({ activeSlug }: { activeSlug: string }) {
  return (
    <nav className="sticky top-0 hidden h-screen w-64 shrink-0 overflow-y-auto border-r border-neutral-900 bg-neutral-950 px-4 py-6 md:block">
      <Link href="/" className="mb-6 block">
        <div className="flex items-baseline gap-2">
          <span className="text-base font-semibold text-neutral-50">a2a</span>
          <span className="text-xs text-neutral-500">docs</span>
        </div>
      </Link>
      <NavGroups activeSlug={activeSlug} />
      <LlmLinks />
    </nav>
  );
}

// Mobile: a disclosure nav at the top of the page. Hidden at md and up.
// Pure <details> — no client JS, works in a server component.
export function MobileNav({ activeSlug }: { activeSlug: string }) {
  return (
    <details className="group border-b border-neutral-900 bg-neutral-950 md:hidden">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 [&::-webkit-details-marker]:hidden">
        <Link href="/" className="flex items-baseline gap-2">
          <span className="text-base font-semibold text-neutral-50">a2a</span>
          <span className="text-xs text-neutral-500">docs</span>
        </Link>
        <span className="flex items-center gap-2 text-sm text-neutral-400">
          Menu
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="transition-transform group-open:rotate-180"
            aria-hidden="true"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </span>
      </summary>
      <div className="max-h-[70vh] overflow-y-auto px-4 pb-4">
        <NavGroups activeSlug={activeSlug} />
        <LlmLinks />
      </div>
    </details>
  );
}
