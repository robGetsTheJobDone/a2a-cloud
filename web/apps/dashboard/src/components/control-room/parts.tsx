import {
  TextInput,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { centsInputValue, sourceBadgeLabel } from "./controlData";

export function SourcePill({ source }: { source: string }) {
  return (
    <StatusBadge>
      {sourceBadgeLabel(source)}
    </StatusBadge>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  plain,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  plain?: boolean;
}) {
  return (
    <label className="text-xs text-ink-muted">
      {label}
      <TextInput
        type="number"
        aria-label={plain ? label : `${label} in dollars`}
        inputMode={plain ? "numeric" : "decimal"}
        min={plain ? 1 : 0}
        step={plain ? 1 : 0.01}
        value={plain ? value : centsInputValue(value)}
        onChange={(event) => {
          const raw = Number(event.target.value || 0);
          onChange(plain ? Math.max(1, Math.round(raw)) : Math.max(0, Math.round(raw * 100)));
        }}
        className="mt-1"
      />
    </label>
  );
}
