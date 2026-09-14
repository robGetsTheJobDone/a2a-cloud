import type { ServiceStatus } from "@/lib/status";

const stateClasses = {
  online: "border-emerald-700/50 bg-emerald-950/30 text-emerald-200",
  degraded: "border-amber-700/50 bg-amber-950/30 text-amber-200",
  offline: "border-red-800/60 bg-red-950/30 text-red-200",
};

export function StatusGrid({ statuses }: { statuses: ServiceStatus[] }) {
  return (
    <section className="rounded-lg border border-line bg-panel" aria-label="Platform status" data-tour="platform-status">
      <div className="grid grid-cols-[minmax(0,1fr)_120px_120px] gap-3 border-b border-line px-4 py-3 text-xs uppercase text-neutral-500">
        <div>Service</div>
        <div>State</div>
        <div>Latency</div>
      </div>
      <div className="divide-y divide-line">
        {statuses.map((service) => (
          <div
            key={service.name}
            data-tour={`service-${service.name}`}
            className="grid grid-cols-[minmax(0,1fr)_120px_120px] items-center gap-3 px-4 py-3"
          >
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-neutral-100">{service.label}</div>
              <div className="mt-1 truncate font-mono text-xs text-neutral-600">{service.url}</div>
            </div>
            <div>
              <span className={`inline-flex rounded-md border px-2 py-1 text-xs font-medium ${stateClasses[service.state]}`}>
                {service.code ?? "down"}
              </span>
            </div>
            <div className="font-mono text-xs text-neutral-400">
              {service.latencyMs === null ? "timeout" : `${service.latencyMs}ms`}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
