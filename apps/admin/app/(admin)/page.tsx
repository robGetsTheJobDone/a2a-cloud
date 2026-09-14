import Link from "next/link";
import { overviewMetrics, sections } from "@/lib/admin-data";
import { getServiceStatuses } from "@/lib/status";
import { StatusGrid } from "@/components/StatusGrid";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const statuses = await getServiceStatuses();
  const pages = Object.values(sections);

  return (
    <div className="space-y-6">
      <header className="border-b border-line pb-5" data-tour="page-title">
        <div className="text-xs uppercase text-neutral-500">Platform</div>
        <h1 className="mt-2 text-3xl font-semibold text-neutral-50">Admin Console</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-400">
          Dedicated operations surface for runtime control, releases, identity, secrets, and audit review.
        </p>
      </header>

      <section className="grid gap-3 md:grid-cols-4" aria-label="Admin metrics">
        {overviewMetrics.map((metric) => (
          <div key={metric.label} className="rounded-lg border border-line bg-panel p-4">
            <div className="text-xs text-neutral-500">{metric.label}</div>
            <div className="mt-2 truncate text-lg font-semibold text-neutral-100">{metric.value}</div>
          </div>
        ))}
      </section>

      <StatusGrid statuses={statuses} />

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" aria-label="Admin pages">
        {pages.map((page) => (
          <Link
            key={page.slug}
            href={`/${page.slug}`}
            className="rounded-lg border border-line bg-panel p-4 transition hover:border-neutral-600"
          >
            <div className="text-xs uppercase text-neutral-500">{page.eyebrow}</div>
            <div className="mt-2 text-lg font-semibold text-neutral-100">{page.label}</div>
            <p className="mt-2 text-sm leading-6 text-neutral-500">{page.description}</p>
          </Link>
        ))}
      </section>
    </div>
  );
}
