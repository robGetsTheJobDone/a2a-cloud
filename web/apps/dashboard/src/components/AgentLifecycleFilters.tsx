import {
  FormField,
  SelectInput,
  TextInput,
  ToolbarButton,
} from "./DashboardChrome";

type AgentLifecycleStatusFilter =
  | "all"
  | "running"
  | "deploying"
  | "failed"
  | "inactive";

type AgentLifecycleVisibilityFilter = "all" | "public" | "private";

type AgentLifecycleProofFilter =
  | "all"
  | "verified"
  | "degraded"
  | "unverified";

type AgentLifecycleRuntimeUpdateFilter =
  | "all"
  | "update-available"
  | "up-to-date";

type AgentLifecycleSort =
  | "created-desc"
  | "created-asc"
  | "name-asc"
  | "name-desc"
  | "status"
  | "proof"
  | "runtime-update";

export type AgentLifecycleFilterValue = {
  query: string;
  status: AgentLifecycleStatusFilter;
  visibility: AgentLifecycleVisibilityFilter;
  proof: AgentLifecycleProofFilter;
  runtimeUpdate: AgentLifecycleRuntimeUpdateFilter;
  sort: AgentLifecycleSort;
};

export type AgentLifecycleFiltersProps = {
  value: AgentLifecycleFilterValue;
  resultCount: number;
  totalCount: number;
  onChange: (next: AgentLifecycleFilterValue) => void;
  disabled?: boolean;
  className?: string;
};

export const DEFAULT_AGENT_LIFECYCLE_FILTERS: AgentLifecycleFilterValue = {
  query: "",
  status: "all",
  visibility: "all",
  proof: "all",
  runtimeUpdate: "all",
  sort: "created-desc",
};

const STATUS_OPTIONS: ReadonlyArray<{
  value: AgentLifecycleStatusFilter;
  label: string;
}> = [
  { value: "all", label: "All statuses" },
  { value: "running", label: "Running" },
  { value: "deploying", label: "Deploying" },
  { value: "failed", label: "Failed" },
  { value: "inactive", label: "Inactive" },
];

const VISIBILITY_OPTIONS: ReadonlyArray<{
  value: AgentLifecycleVisibilityFilter;
  label: string;
}> = [
  { value: "all", label: "All visibility" },
  { value: "public", label: "Public" },
  { value: "private", label: "Private" },
];

const PROOF_OPTIONS: ReadonlyArray<{
  value: AgentLifecycleProofFilter;
  label: string;
}> = [
  { value: "all", label: "All proof" },
  { value: "verified", label: "Verified" },
  { value: "degraded", label: "Degraded" },
  { value: "unverified", label: "Unverified" },
];

const RUNTIME_UPDATE_OPTIONS: ReadonlyArray<{
  value: AgentLifecycleRuntimeUpdateFilter;
  label: string;
}> = [
  { value: "all", label: "All runtimes" },
  { value: "update-available", label: "Update available" },
  { value: "up-to-date", label: "Up to date" },
];

const SORT_OPTIONS: ReadonlyArray<{
  value: AgentLifecycleSort;
  label: string;
}> = [
  { value: "created-desc", label: "Newest first" },
  { value: "created-asc", label: "Oldest first" },
  { value: "name-asc", label: "Name A-Z" },
  { value: "name-desc", label: "Name Z-A" },
  { value: "status", label: "Status" },
  { value: "proof", label: "Proof" },
  { value: "runtime-update", label: "Runtime update" },
];

export function AgentLifecycleFilters({
  value,
  resultCount,
  totalCount,
  onChange,
  disabled = false,
  className,
}: AgentLifecycleFiltersProps) {
  const hasActiveFilters =
    value.query.trim().length > 0 ||
    value.status !== DEFAULT_AGENT_LIFECYCLE_FILTERS.status ||
    value.visibility !== DEFAULT_AGENT_LIFECYCLE_FILTERS.visibility ||
    value.proof !== DEFAULT_AGENT_LIFECYCLE_FILTERS.proof ||
    value.runtimeUpdate !== DEFAULT_AGENT_LIFECYCLE_FILTERS.runtimeUpdate ||
    value.sort !== DEFAULT_AGENT_LIFECYCLE_FILTERS.sort;

  function update(patch: Partial<AgentLifecycleFilterValue>) {
    onChange({ ...value, ...patch });
  }

  function clearFilters() {
    onChange({ ...DEFAULT_AGENT_LIFECYCLE_FILTERS });
  }

  return (
    <section
      className={["flex flex-col gap-2", className].filter(Boolean).join(" ")}
      aria-label="Agent lifecycle filters"
    >
      <TextInput
        type="search"
        value={value.query}
        onChange={(event) => update({ query: event.target.value })}
        placeholder="Name, tool, status..."
        disabled={disabled}
        aria-label="Search agents"
      />

      <div className="grid grid-cols-2 gap-2">
        <FilterSelect
          label="Status"
          value={value.status}
          options={STATUS_OPTIONS}
          disabled={disabled}
          onChange={(status) => update({ status })}
        />
        <FilterSelect
          label="Visibility"
          value={value.visibility}
          options={VISIBILITY_OPTIONS}
          disabled={disabled}
          onChange={(visibility) => update({ visibility })}
        />
        <FilterSelect
          label="Proof"
          value={value.proof}
          options={PROOF_OPTIONS}
          disabled={disabled}
          onChange={(proof) => update({ proof })}
        />
        <FilterSelect
          label="Runtime update"
          value={value.runtimeUpdate}
          options={RUNTIME_UPDATE_OPTIONS}
          disabled={disabled}
          onChange={(runtimeUpdate) => update({ runtimeUpdate })}
        />
        <FilterSelect
          label="Sort"
          value={value.sort}
          options={SORT_OPTIONS}
          disabled={disabled}
          onChange={(sort) => update({ sort })}
        />
        <div className="flex items-end">
          <ToolbarButton
            type="button"
            onClick={clearFilters}
            disabled={disabled || !hasActiveFilters}
            size="xs"
            className="w-full"
          >
            Clear
          </ToolbarButton>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-muted">
        <span>
          Showing{" "}
          <span className="font-mono text-ink-soft">{resultCount}</span> of{" "}
          <span className="font-mono text-ink-soft">{totalCount}</span>
        </span>
        {hasActiveFilters && (
          <span className="text-ink-faint">Filters applied</span>
        )}
      </div>
    </section>
  );
}

function FilterSelect<TValue extends string>({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: TValue;
  options: ReadonlyArray<{ value: TValue; label: string }>;
  disabled: boolean;
  onChange: (value: TValue) => void;
}) {
  return (
    <FormField label={label}>
      <SelectInput
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as TValue)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </SelectInput>
    </FormField>
  );
}
