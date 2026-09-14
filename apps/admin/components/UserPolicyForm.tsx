"use client";

import { useState } from "react";
import type { ControlPolicy } from "@/lib/cp";

type Props = {
  userId: number;
  initial: ControlPolicy | null;
};

const fallback: ControlPolicy = {
  monthly_budget_cents: 5000,
  run_budget_cents: 500,
  max_agent_calls_per_run: 8,
  require_approval_for_file_writes: false,
  deny_external_network: false,
  only_approved_agents: false,
  pii_safe_mode: false,
  approved_agents: [],
};

export function UserPolicyForm({ userId, initial }: Props) {
  const [policy, setPolicy] = useState<ControlPolicy>(initial || fallback);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setStatus(null);
    try {
      const resp = await fetch(`/api/admin/users/${userId}/policy`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(policy),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const out = await resp.json();
      setPolicy(out.policy);
      setStatus("Saved");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <NumberField
          label="Monthly"
          value={policy.monthly_budget_cents}
          onChange={(monthly_budget_cents) => setPolicy({ ...policy, monthly_budget_cents })}
        />
        <NumberField
          label="Run"
          value={policy.run_budget_cents}
          onChange={(run_budget_cents) => setPolicy({ ...policy, run_budget_cents })}
        />
        <NumberField
          label="Handoffs"
          value={policy.max_agent_calls_per_run}
          onChange={(max_agent_calls_per_run) => setPolicy({ ...policy, max_agent_calls_per_run })}
          min={1}
          max={100}
        />
        <button
          type="button"
          disabled={busy}
          onClick={save}
          className="h-8 rounded-md border border-neutral-700 bg-neutral-900 px-3 text-xs text-neutral-100 disabled:opacity-50"
        >
          Save
        </button>
        {status && <span className="max-w-[14rem] truncate text-xs text-neutral-500">{status}</span>}
      </div>
      <div className="flex flex-wrap gap-3">
        <CheckField
          label="Approve writes"
          checked={policy.require_approval_for_file_writes}
          onChange={(require_approval_for_file_writes) =>
            setPolicy({ ...policy, require_approval_for_file_writes })
          }
        />
        <CheckField
          label="Deny network"
          checked={policy.deny_external_network}
          onChange={(deny_external_network) => setPolicy({ ...policy, deny_external_network })}
        />
        <CheckField
          label="Allowlist only"
          checked={policy.only_approved_agents}
          onChange={(only_approved_agents) => setPolicy({ ...policy, only_approved_agents })}
        />
        <CheckField
          label="PII safe"
          checked={policy.pii_safe_mode}
          onChange={(pii_safe_mode) => setPolicy({ ...policy, pii_safe_mode })}
        />
      </div>
      <label className="block max-w-lg">
        <span className="mb-1 block text-[10px] uppercase text-neutral-500">Approved agents</span>
        <input
          value={policy.approved_agents.join(", ")}
          onChange={(event) =>
            setPolicy({
              ...policy,
              approved_agents: event.target.value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean),
            })
          }
          placeholder="agent-a, agent-b"
          className="h-8 w-full rounded-md border border-line bg-neutral-950 px-2 text-xs text-neutral-100 outline-none placeholder:text-neutral-600 focus:border-neutral-600"
        />
      </label>
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  min = 0,
  max = 10_000_000,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] uppercase text-neutral-500">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => {
          const next = Number(event.target.value);
          onChange(Number.isFinite(next) ? Math.max(min, Math.min(max, Math.floor(next))) : min);
        }}
        className="h-8 w-24 rounded-md border border-line bg-neutral-950 px-2 text-xs text-neutral-100 outline-none focus:border-neutral-600"
      />
    </label>
  );
}

function CheckField({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-neutral-400">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3.5 w-3.5 rounded border-line bg-neutral-950"
      />
      <span>{label}</span>
    </label>
  );
}
