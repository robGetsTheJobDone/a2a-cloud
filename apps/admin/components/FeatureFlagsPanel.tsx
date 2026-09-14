"use client";

import { useState } from "react";
import type { AdminFeatureFlag, AdminFeatureFlagInput } from "@/lib/cp";

type Props = {
  initialFlags: AdminFeatureFlag[];
};

type FlagDraft = AdminFeatureFlagInput;

const emptyDraft: FlagDraft = {
  key: "",
  label: "",
  description: "",
  default_enabled: false,
};

export function FeatureFlagsPanel({ initialFlags }: Props) {
  const [flags, setFlags] = useState<AdminFeatureFlag[]>(initialFlags);
  const [draft, setDraft] = useState<FlagDraft>(emptyDraft);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  async function saveFlag() {
    const nextDraft: FlagDraft = {
      key: draft.key.trim().toLowerCase(),
      label: draft.label.trim(),
      description: draft.description?.trim() || null,
      default_enabled: draft.default_enabled,
    };
    setBusyKey(nextDraft.key || "new");
    setStatus(null);
    try {
      const resp = await fetch("/api/admin/feature-flags", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nextDraft),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const saved = (await resp.json()) as AdminFeatureFlag;
      setFlags((current) => {
        const without = current.filter((flag) => flag.key !== saved.key);
        return [...without, saved].sort((a, b) => a.key.localeCompare(b.key));
      });
      setDraft(emptyDraft);
      setStatus("Saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyKey(null);
    }
  }

  async function removeFlag(key: string) {
    setBusyKey(key);
    setStatus(null);
    try {
      const resp = await fetch(`/api/admin/feature-flags/${encodeURIComponent(key)}`, {
        method: "DELETE",
      });
      if (!resp.ok) throw new Error(await resp.text());
      setFlags((current) => current.filter((flag) => flag.key !== key));
      if (draft.key === key) setDraft(emptyDraft);
      setStatus("Removed");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <section className="rounded-lg border border-line bg-panel p-5" aria-label="Feature flag store">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="max-w-3xl">
          <div className="text-sm font-semibold text-neutral-100">Feature flags</div>
          <p className="mt-2 text-sm leading-6 text-neutral-400">
            Create, edit, and remove user-level flags. The dashboard nav gates use
            dashboard.simulations, dashboard.bounties, and dashboard.organization.
          </p>
        </div>
        {status && <div className="max-w-md truncate text-xs text-neutral-500">{status}</div>}
      </div>

      <div className="mt-5 grid gap-3 lg:grid-cols-[180px_minmax(180px,260px)_minmax(220px,1fr)_140px_90px]">
        <TextField
          label="Key"
          value={draft.key}
          placeholder="dashboard.simulations"
          onChange={(key) => setDraft({ ...draft, key })}
        />
        <TextField
          label="Label"
          value={draft.label}
          placeholder="Dashboard simulations nav"
          onChange={(label) => setDraft({ ...draft, label })}
        />
        <TextField
          label="Description"
          value={draft.description || ""}
          placeholder="Shown only for selected users"
          onChange={(description) => setDraft({ ...draft, description })}
        />
        <label className="flex items-end gap-2 pb-2 text-xs text-neutral-400">
          <input
            type="checkbox"
            checked={draft.default_enabled}
            onChange={(event) => setDraft({ ...draft, default_enabled: event.target.checked })}
            className="h-3.5 w-3.5 rounded border-line bg-neutral-950"
          />
          <span>Default on</span>
        </label>
        <button
          type="button"
          onClick={saveFlag}
          disabled={busyKey !== null || !draft.key.trim() || !draft.label.trim()}
          className="h-8 self-end rounded-md border border-neutral-700 bg-neutral-900 px-3 text-xs text-neutral-100 disabled:opacity-50"
        >
          Save
        </button>
      </div>

      <div className="mt-5 divide-y divide-line rounded-md border border-line">
        {flags.length ? (
          flags.map((flag) => (
            <div
              key={flag.key}
              className="grid gap-3 px-3 py-3 text-sm md:grid-cols-[minmax(190px,0.8fr)_minmax(180px,1fr)_90px_90px_150px]"
            >
              <div className="min-w-0">
                <div className="truncate font-medium text-neutral-100">{flag.key}</div>
                <div className="mt-1 truncate text-xs text-neutral-500">{flag.description}</div>
              </div>
              <div className="truncate text-neutral-300">{flag.label}</div>
              <div className="text-xs text-neutral-500">
                {flag.default_enabled ? "Default on" : "Default off"}
              </div>
              <div className="text-xs text-neutral-500">{flag.assigned_user_count} users</div>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setDraft({
                      key: flag.key,
                      label: flag.label,
                      description: flag.description || "",
                      default_enabled: flag.default_enabled,
                    })
                  }
                  className="h-8 rounded-md border border-neutral-800 bg-neutral-950 px-2 text-xs text-neutral-300"
                >
                  Edit
                </button>
                <button
                  type="button"
                  onClick={() => removeFlag(flag.key)}
                  disabled={busyKey === flag.key}
                  className="h-8 rounded-md border border-red-900/60 bg-red-950/20 px-2 text-xs text-red-200 disabled:opacity-50"
                >
                  Remove
                </button>
              </div>
            </div>
          ))
        ) : (
          <div className="px-3 py-5 text-sm text-neutral-500">No feature flags yet.</div>
        )}
      </div>
    </section>
  );
}

function TextField({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase text-neutral-500">{label}</span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full rounded-md border border-line bg-neutral-950 px-2 text-xs text-neutral-100 outline-none placeholder:text-neutral-600 focus:border-neutral-600"
      />
    </label>
  );
}
