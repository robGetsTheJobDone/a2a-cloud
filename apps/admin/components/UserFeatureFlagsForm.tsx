"use client";

import { useMemo, useState } from "react";
import type { AdminFeatureFlag } from "@/lib/cp";

type Props = {
  userId: number;
  availableFlags: AdminFeatureFlag[];
  initialEnabledKeys: string[];
};

export function UserFeatureFlagsForm({
  userId,
  availableFlags,
  initialEnabledKeys,
}: Props) {
  const [enabledKeys, setEnabledKeys] = useState<string[]>(initialEnabledKeys);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const enabled = useMemo(() => new Set(enabledKeys), [enabledKeys]);

  function toggle(key: string, checked: boolean) {
    setEnabledKeys((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return Array.from(next).sort();
    });
  }

  async function save() {
    setBusy(true);
    setStatus(null);
    try {
      const resp = await fetch(`/api/admin/users/${userId}/feature-flags`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled_keys: enabledKeys }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const out = (await resp.json()) as { enabled_keys: string[] };
      setEnabledKeys(out.enabled_keys);
      setStatus("Saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  if (!availableFlags.length) {
    return <div className="text-xs text-neutral-500">No flags</div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {availableFlags.map((flag) => (
          <label
            key={flag.key}
            className="inline-flex items-center gap-2 rounded-md border border-line bg-neutral-950 px-2 py-1 text-xs text-neutral-400"
            title={flag.description || flag.label}
          >
            <input
              type="checkbox"
              checked={enabled.has(flag.key)}
              onChange={(event) => toggle(flag.key, event.target.checked)}
              className="h-3.5 w-3.5 rounded border-line bg-neutral-950"
            />
            <span className="max-w-[11rem] truncate">{flag.key}</span>
          </label>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={save}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-900 px-3 text-xs text-neutral-100 disabled:opacity-50"
        >
          Save flags
        </button>
        {status && <span className="max-w-[12rem] truncate text-xs text-neutral-500">{status}</span>}
      </div>
    </div>
  );
}
