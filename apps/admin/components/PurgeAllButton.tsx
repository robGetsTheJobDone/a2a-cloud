"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

type Props = {
  disabled?: boolean;
  userCount: number;
  agentCount: number;
  repoCount: number;
};

export function PurgeAllButton({ disabled, userCount, agentCount, repoCount }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

  const onClick = () => {
    if (disabled) return;
    const confirmed = window.confirm(
      `Delete ${userCount} users, ${agentCount} agents, and ${repoCount} managed repos? This cannot be undone.`,
    );
    if (!confirmed) return;
    setError(null);
    startTransition(async () => {
      try {
        const resp = await fetch("/api/admin/users/purge", {
          method: "DELETE",
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => null);
          setError(body?.error || `purge failed: ${resp.status}`);
          return;
        }
        router.refresh();
      } catch (e) {
        setError(`purge failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    });
  };

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled || pending}
        className={`rounded-md border border-red-800/60 bg-red-950/30 px-4 py-2 text-sm font-medium text-red-100 hover:border-red-700 hover:bg-red-950/50 ${
          disabled || pending ? "cursor-not-allowed opacity-50" : ""
        }`}
      >
        {pending ? "Purging..." : "Delete all"}
      </button>
      {error && (
        <div className="max-w-xl rounded-md border border-red-800/60 bg-red-950/30 px-3 py-2 text-xs leading-5 text-red-200">
          {error}
        </div>
      )}
    </div>
  );
}
