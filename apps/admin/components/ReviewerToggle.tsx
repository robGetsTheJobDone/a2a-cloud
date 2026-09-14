"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

type Props = {
  initialValue: boolean;
  updatedBy: string | null;
  updatedAt: string | null;
};

export function ReviewerToggle({ initialValue, updatedBy, updatedAt }: Props) {
  const [value, setValue] = useState(initialValue);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

  const onToggle = () => {
    const next = !value;
    setError(null);
    startTransition(async () => {
      try {
        const resp = await fetch("/api/admin/platform/settings/reviewer_enabled", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: next }),
        });
        if (!resp.ok) {
          setError(`update failed: ${resp.status}`);
          return;
        }
        setValue(next);
        router.refresh();
      } catch (e) {
        setError(`update failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    });
  };

  return (
    <div className="rounded-lg border border-line bg-panel p-5">
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-neutral-100">
            Pre-deploy reviewer
          </div>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-neutral-400">
            When on, every successful <code>from-tarball</code> upload triggers
            an async advisory review by <code>agent-reviewer</code>. Findings
            land on <code>AgentReviewRun</code> and a{" "}
            <code>stage=&quot;review&quot;</code> deployment event. Deploys are
            never blocked by review verdicts.
          </p>
          {error && (
            <div className="mt-3 rounded-md border border-red-800/60 bg-red-950/30 px-3 py-2 text-xs text-red-200">
              {error}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onToggle}
          disabled={pending}
          aria-pressed={value}
          className={`relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border transition-colors ${
            value
              ? "border-emerald-700/60 bg-emerald-700/30"
              : "border-neutral-700 bg-neutral-900"
          } ${pending ? "opacity-50" : ""}`}
        >
          <span
            className={`inline-block h-5 w-5 transform rounded-full bg-neutral-100 transition-transform ${
              value ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-4 border-t border-line pt-4 text-xs">
        <div>
          <div className="uppercase text-neutral-500">Current</div>
          <div className="mt-1 font-medium text-neutral-200">
            {value ? "Enabled" : "Disabled"}
          </div>
        </div>
        <div>
          <div className="uppercase text-neutral-500">Last changed</div>
          <div className="mt-1 font-medium text-neutral-200">
            {updatedAt
              ? `${new Date(updatedAt).toLocaleString()}${updatedBy ? ` (${updatedBy})` : ""}`
              : "Never (using default)"}
          </div>
        </div>
      </div>
    </div>
  );
}
