import type { AgentOpenApiPreview } from "../api";

const FIELD_LABEL_CLASS_NAME =
  "text-[10px] uppercase tracking-wider text-ink-faint";

export function OpenApiPreviewCard({
  preview,
  busy,
  onPreview,
  existingAgentName,
}: {
  preview: AgentOpenApiPreview | null;
  busy: boolean;
  onPreview: () => void;
  existingAgentName: string | null;
}) {
  const setupCount = preview?.consumer_setup.fields.length || 0;
  const metrics = preview
    ? [
        { label: "ops", value: preview.operation_count },
        { label: "setup", value: setupCount },
        { label: "files", value: preview.source_files.length },
      ]
    : [];

  return (
    <div className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <span className={FIELD_LABEL_CLASS_NAME}>preview</span>
          <div className="mt-1 text-sm font-medium text-ink">
            {preview ? preview.name : "OpenAPI source"}
          </div>
        </div>
        <button
          type="button"
          onClick={onPreview}
          disabled={busy}
          className="rounded-md border border-runtime-line px-3 py-1.5 text-xs text-ink-soft hover:border-runtime-line-strong disabled:opacity-50"
        >
          {busy ? "loading..." : "preview"}
        </button>
      </div>

      {preview && (
        <div className="mt-3 space-y-3">
          <dl className="grid grid-cols-3 overflow-hidden rounded-md border border-runtime-line-soft/60 text-xs">
            {metrics.map((metric, index) => (
              <div
                key={metric.label}
                className={
                  "min-w-0 px-2 py-2 " +
                  (index < metrics.length - 1 ? "border-r border-runtime-line-soft/60" : "")
                }
              >
                <dt className="text-ink-muted">{metric.label}</dt>
                <dd className="mt-1 truncate font-mono text-ink">
                  {metric.value}
                </dd>
              </div>
            ))}
          </dl>
          <div className="max-h-52 overflow-auto rounded-md border border-runtime-line-soft/60">
            {preview.operations.slice(0, 8).map((op) => (
              <div
                key={op.operation_id}
                className="grid grid-cols-[64px_minmax(0,1fr)] gap-2 border-b border-runtime-line-soft/60 px-2 py-2 last:border-b-0"
              >
                <span
                  className={`rounded-md px-1.5 py-0.5 text-center font-mono text-[10px] ${
                    op.destructive
                      ? "bg-signal-authority/10 text-signal-authority"
                      : "bg-signal-live/10 text-signal-live"
                  }`}
                >
                  {op.method}
                </span>
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs text-ink-soft">
                    {op.path}
                  </div>
                  <div className="truncate text-xs text-ink-muted">
                    {op.skill_name}
                  </div>
                </div>
              </div>
            ))}
          </div>
          {preview.warnings.length > 0 && (
            <div className="rounded-md border border-signal-authority/45 bg-signal-authority/12 px-2 py-2 text-xs text-signal-authority">
              {preview.warnings[0]}
            </div>
          )}
          {existingAgentName && (
            <div className="rounded-md border border-signal-authority/45 bg-signal-authority/12 px-2 py-2 text-xs text-signal-authority">
              Existing agent found. Generating will ask before refreshing it from
              the current OpenAPI spec.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
