import Link from "next/link";
import type { AdminSection } from "@/lib/admin-data";

const toneClasses = {
  neutral: "border-neutral-800 bg-neutral-950 text-neutral-300",
  green: "border-emerald-700/50 bg-emerald-950/30 text-emerald-200",
  amber: "border-amber-700/50 bg-amber-950/30 text-amber-200",
  red: "border-red-800/60 bg-red-950/30 text-red-200",
  cyan: "border-cyan-700/50 bg-cyan-950/30 text-cyan-200",
};

export function AdminSectionPage({ section }: { section: AdminSection }) {
  return (
    <div className="space-y-6">
      <header className="border-b border-line pb-5" data-tour="page-title">
        <div className="text-xs uppercase text-neutral-500">{section.eyebrow}</div>
        <h1 className="mt-2 text-3xl font-semibold text-neutral-50">{section.title}</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-neutral-400">{section.description}</p>
      </header>

      <section className="grid gap-3 md:grid-cols-4" aria-label={`${section.label} metrics`}>
        {section.metrics.map((metric) => (
          <div key={metric.label} className="rounded-lg border border-line bg-panel p-4">
            <div className="text-xs text-neutral-500">{metric.label}</div>
            <div className="mt-2 truncate text-lg font-semibold text-neutral-100">{metric.value}</div>
          </div>
        ))}
      </section>

      <section className="rounded-lg border border-line bg-panel" aria-label={`${section.label} work queue`}>
        <div className="grid grid-cols-[minmax(0,1.2fr)_140px_120px] gap-3 border-b border-line px-4 py-3 text-xs uppercase text-neutral-500">
          <div>Work</div>
          <div>Owner</div>
          <div>Status</div>
        </div>
        <div className="divide-y divide-line">
          {section.work.map((item) => (
            <div
              key={item.title}
              className="grid grid-cols-1 gap-3 px-4 py-4 md:grid-cols-[minmax(0,1.2fr)_140px_120px]"
            >
              <div className="min-w-0">
                <div className="font-medium text-neutral-100">{item.title}</div>
                <div className="mt-1 text-sm leading-6 text-neutral-500">{item.detail}</div>
              </div>
              <div className="text-sm text-neutral-300">{item.owner}</div>
              <div>
                <span
                  className={`inline-flex rounded-md border px-2 py-1 text-xs font-medium ${
                    toneClasses[item.tone || "neutral"]
                  }`}
                >
                  {item.state}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="flex flex-wrap gap-2" aria-label={`${section.label} links`}>
        {section.links.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className="rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm font-medium text-neutral-200 hover:border-neutral-500 hover:text-neutral-50"
          >
            {link.label}
          </Link>
        ))}
      </section>
    </div>
  );
}
