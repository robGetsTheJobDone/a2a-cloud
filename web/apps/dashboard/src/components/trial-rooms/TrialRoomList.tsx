import {
  InlineAlert,
  LoadingState,
  SelectableSurfaceButton,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import { type TrialRoom } from "../../api";

/**
 * TrialRoomList — sidebar rail of trial rooms with the "New trial" affordance.
 *
 * Mandate B/C: selection is hoisted state, not a route. Rows are buttons that
 * flip the selected slug in place (no remount/refetch) and "New trial" opens
 * the create sheet via onCreate instead of redirecting to /trials/new.
 */
export function TrialRoomList({
  rooms,
  selected,
  creating,
  err,
  totals,
  onSelect,
  onCreate,
}: {
  rooms: TrialRoom[] | null;
  selected: TrialRoom | null;
  creating: boolean;
  err: string | null;
  totals: { total: number; runs: number; deployed: number };
  onSelect: (slug: string) => void;
  onCreate: () => void;
}) {
  return (
    <div className="flex min-h-0 min-w-0 flex-col">
      <div className="sticky top-0 z-10 flex items-center justify-between gap-2 border-b border-runtime-line-soft/70 bg-runtime-bg/95 px-3 py-2 backdrop-blur">
        <div className="flex min-w-0 items-baseline gap-3 font-mono text-[11px] text-ink-faint">
          <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-ink-muted">
            Trials
          </span>
          <span>
            <span className="text-ink">{totals.total}</span> rooms
          </span>
          <span>
            <span className="text-ink">{totals.runs}</span> runs
          </span>
          <span className="text-signal-protocol">
            <span className="text-signal-protocol">{totals.deployed}</span> picked
          </span>
        </div>
        <ToolbarButton
          type="button"
          data-onboarding-target="trials-new"
          onClick={onCreate}
          variant="primary"
          size="xs"
        >
          New trial
        </ToolbarButton>
      </div>

      {err && (
        <div role="alert" className="px-3 pt-3">
          <InlineAlert tone="red">{err}</InlineAlert>
        </div>
      )}

      <div className="min-h-0 flex-1 space-y-2 overflow-auto p-2">
        {rooms === null ? (
          <LoadingState label="Loading trial rooms..." />
        ) : rooms.length === 0 ? (
          <SelectableSurfaceButton
            onClick={onCreate}
            selected={creating}
            className="border-dashed bg-runtime-panel/20 text-sm text-ink-dim"
          >
            No trials yet. Create a private room with real files, run agents,
            and compare receipts.
          </SelectableSurfaceButton>
        ) : (
          rooms.map((room) => (
            <SelectableSurfaceButton
              key={room.slug}
              onClick={() => onSelect(room.slug)}
              selected={!creating && selected?.slug === room.slug}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-ink">
                  {room.title}
                </span>
                <StateBadge status={room.status} />
              </div>
              <div className="mt-2 line-clamp-2 text-xs text-ink-muted">
                {room.goal}
              </div>
              <div className="mt-3 flex items-center gap-3 text-[11px] text-ink-faint">
                <span>{room.input_paths.length} files</span>
                <span>{room.runs.length} runs</span>
                {room.selected_run_id && <span>winner selected</span>}
              </div>
            </SelectableSurfaceButton>
          ))
        )}
      </div>
    </div>
  );
}
