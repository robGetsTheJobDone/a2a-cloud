import { useEffect, useState } from "react";
import {
  rateTrialRun,
  selectTrialWinner,
  type TrialRoom,
  type TrialRun,
} from "../../api";
import { RunDetailsPanel } from "../RunDetailsPanel";
import {
  CodeBlock,
  EmptyState,
  FormField,
  InlineAlert,
  SegmentedButton,
  SegmentedControl,
  SummaryMetric,
  SurfacePanel,
  TextArea as FormTextArea,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import { receiptId } from "../trialRoomUtils";
import { FileOps, RunEvidenceStrip } from "./shared";
import {
  TRIAL_RUN_DETAIL_SECTIONS,
  changedFileCount,
  clampScore,
  fmtMs,
  trialRunDetails,
  type RoomPatch,
  type TrialRunDetailSectionId,
} from "./helpers";

/**
 * TrialRunDetailView — the body of the run-detail DetailSheet (mandate C). The
 * sheet shell supplies the title/close affordance, so this renders only the
 * section nav + the active section. Section selection is local hoisted state
 * driven by onSectionChange (no route redirect, mandate B).
 */
export function TrialRunDetailView({
  room,
  run,
  requestedRunId,
  activeSection,
  onSectionChange,
  onRoomChange,
}: {
  room: TrialRoom;
  run: TrialRun | null;
  requestedRunId: string;
  activeSection: TrialRunDetailSectionId;
  onSectionChange: (section: TrialRunDetailSectionId) => void;
  onRoomChange: RoomPatch;
}) {
  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-center gap-2 border-b border-runtime-line-soft/60 pb-3">
        {run && (
          <StateBadge
            status={run.status}
            live={run.status === "running" || run.status === "evaluating"}
          />
        )}
        <span className="break-all font-mono text-[11px] text-ink-faint">
          {room.slug} / {requestedRunId}
        </span>
      </div>

      {run ? (
        <>
          <TrialRunDetailNav
            activeSection={activeSection}
            onSectionChange={onSectionChange}
          />
          {activeSection === "evidence" ? (
            <RunDetailsPanel details={trialRunDetails(room, run)} />
          ) : activeSection === "review" ? (
            <TrialRunReviewPanel
              room={room}
              run={run}
              selected={run.id === room.selected_run_id}
              onRoomChange={onRoomChange}
              sticky={false}
            />
          ) : activeSection === "receipt" ? (
            <TrialRunReceiptPanel run={run} />
          ) : (
            <TrialRunOverviewPanel
              room={room}
              run={run}
              selected={run.id === room.selected_run_id}
            />
          )}
        </>
      ) : (
        <EmptyState
          title="Run not found"
          description="This receipt is no longer attached to the selected trial room."
        />
      )}
    </div>
  );
}

function TrialRunDetailNav({
  activeSection,
  onSectionChange,
}: {
  activeSection: TrialRunDetailSectionId;
  onSectionChange: (section: TrialRunDetailSectionId) => void;
}) {
  return (
    <SegmentedControl
      role="tablist"
      aria-label="Trial run detail sections"
      className="flex gap-1 overflow-x-auto bg-runtime-panel/60"
    >
      {TRIAL_RUN_DETAIL_SECTIONS.map((section) => (
        <SegmentedButton
          key={section.id}
          role="tab"
          aria-selected={activeSection === section.id}
          selected={activeSection === section.id}
          onClick={() => onSectionChange(section.id)}
          className="whitespace-nowrap"
        >
          {section.label}
        </SegmentedButton>
      ))}
    </SegmentedControl>
  );
}

function TrialRunOverviewPanel({
  room,
  run,
  selected,
}: {
  room: TrialRoom;
  run: TrialRun;
  selected: boolean;
}) {
  return (
    <SurfacePanel as="section" className="space-y-4 bg-runtime-bg p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StateBadge
              status={run.status}
              live={run.status === "running" || run.status === "evaluating"}
            />
            {selected && <StatusBadge tone="emerald">winner</StatusBadge>}
          </div>
          <h3 className="mt-2 break-words text-lg font-semibold text-ink">
            {run.agent_name}.{run.skill_name}
          </h3>
          {run.summary && (
            <p className="mt-2 text-sm leading-relaxed text-ink-soft">
              {run.summary}
            </p>
          )}
          {run.error && (
            <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
              {run.error}
            </InlineAlert>
          )}
        </div>
        <div className="grid w-full grid-cols-2 gap-2 text-xs sm:w-auto sm:min-w-[260px]">
          <SummaryMetric label="score" value={run.score} size="compact" />
          <SummaryMetric label="files" value={changedFileCount(run)} size="compact" />
          <SummaryMetric label="runtime" value={fmtMs(run.elapsed_ms)} size="compact" />
          <SummaryMetric label="grant" value={run.grant_id ? run.grant_id.slice(0, 12) : "-"} size="compact" />
        </div>
      </div>
      <RunEvidenceStrip room={room} run={run} />
      {run.evaluator_notes && (
        <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-sm text-ink-soft">
          {run.evaluator_notes}
        </SurfacePanel>
      )}
    </SurfacePanel>
  );
}

function TrialRunReceiptPanel({ run }: { run: TrialRun }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">
          Receipt
        </div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          Raw signed receipt payload attached to this candidate run.
        </p>
      </div>
      <CodeBlock className="max-h-[620px] text-xs">
        {JSON.stringify(run.receipt_json || {}, null, 2)}
      </CodeBlock>
    </SurfacePanel>
  );
}

export function TrialRunCard({
  room,
  run,
  rank,
  topScore,
  selected,
  onRoomChange,
  onOpenRun,
}: {
  room: TrialRoom;
  run: TrialRun;
  rank: number;
  topScore: number;
  selected: boolean;
  onRoomChange: RoomPatch;
  onOpenRun: (runId: number | string, section: TrialRunDetailSectionId) => void;
}) {
  const [busySelect, setBusySelect] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function selectWinner() {
    setBusySelect(true);
    setErr(null);
    try {
      onRoomChange(await selectTrialWinner(room.slug, run.id));
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusySelect(false);
    }
  }

  return (
    <>
      <SurfacePanel
        as="article"
        className={
          "min-w-0 p-4 " +
          (selected
            ? "border-signal-live/45 bg-signal-live/12"
            : "border-runtime-line-soft/60 bg-runtime-bg")
        }
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <StateBadge
                status={run.status}
                live={run.status === "running" || run.status === "evaluating"}
              />
              {selected && (
                <StatusBadge tone="emerald">
                  winner
                </StatusBadge>
              )}
              {rank === 1 && run.score === topScore && !selected && (
                <StatusBadge tone="amber">
                  top score
                </StatusBadge>
              )}
              <span className="min-w-0 break-all font-mono text-sm text-ink">
                {run.agent_name}.{run.skill_name}
              </span>
              {run.grant_id && (
                <span className="text-[11px] text-ink-faint">
                  grant <span className="font-mono">{run.grant_id.slice(0, 8)}</span>
                </span>
              )}
            </div>
            {run.summary && (
              <div className="mt-2 text-sm text-ink-soft">{run.summary}</div>
            )}
            {run.evaluator_notes && (
              <div className="mt-1 text-xs text-ink-muted">{run.evaluator_notes}</div>
            )}
          </div>
          <div className="grid w-full grid-cols-3 gap-2 text-xs sm:w-auto sm:min-w-[180px]">
            <SummaryMetric label="score" value={`${run.score}`} />
            <SummaryMetric label="files" value={changedFileCount(run)} />
            <SummaryMetric label="time" value={fmtMs(run.elapsed_ms)} />
          </div>
        </div>

        <RunEvidenceStrip room={room} run={run} />

        {run.file_ops.length > 0 && <FileOps ops={run.file_ops} />}

        <div className="mt-4 flex flex-wrap gap-2">
          <ToolbarButton
            type="button"
            onClick={() => onOpenRun(run.id, "review")}
            size="xs"
            variant="primary"
          >
            Review run
          </ToolbarButton>
          <ToolbarButton
            onClick={selectWinner}
            disabled={busySelect || run.status === "running"}
            size="xs"
            variant={selected ? "secondary" : "primary"}
          >
            {busySelect ? "selecting..." : "select as winner"}
          </ToolbarButton>
        </div>

        {err && (
          <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
            {err}
          </InlineAlert>
        )}
      </SurfacePanel>
    </>
  );
}

function TrialRunReviewPanel({
  room,
  run,
  selected,
  sticky = true,
  onRoomChange,
}: {
  room: TrialRoom;
  run: TrialRun;
  selected: boolean;
  sticky?: boolean;
  onRoomChange: RoomPatch;
}) {
  const [score, setScore] = useState(run.score);
  const [notes, setNotes] = useState(run.evaluator_notes || "");
  const [saving, setSaving] = useState(false);
  const [busySelect, setBusySelect] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setScore(run.score);
    setNotes(run.evaluator_notes || "");
    setErr(null);
  }, [run.evaluator_notes, run.id, run.score]);

  async function saveReview(passed?: boolean) {
    setSaving(true);
    setErr(null);
    try {
      onRoomChange(await rateTrialRun(room.slug, run.id, {
        score,
        evaluator_notes: notes,
        passed,
      }));
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSaving(false);
    }
  }

  async function selectWinner() {
    setBusySelect(true);
    setErr(null);
    try {
      onRoomChange(await selectTrialWinner(room.slug, run.id));
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusySelect(false);
    }
  }

  return (
    <SurfacePanel
      as="section"
      className={`space-y-4 bg-runtime-bg p-4 ${sticky ? "xl:sticky xl:top-5" : ""}`}
    >
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-xs uppercase text-ink-muted">
            Review decision
          </div>
          <StateBadge
            status={run.status}
            live={run.status === "running" || run.status === "evaluating"}
          />
          {selected && <StatusBadge tone="emerald">winner</StatusBadge>}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          Score this candidate, record evaluator notes, and choose the room winner.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <SummaryMetric label="score" value={run.score} size="compact" />
        <SummaryMetric label="files" value={changedFileCount(run)} size="compact" />
        <SummaryMetric label="receipt" value={receiptId(run).slice(0, 12) || "-"} size="compact" />
        <SummaryMetric label="runtime" value={fmtMs(run.elapsed_ms)} size="compact" />
      </div>

      <FormField label="Score" description="0 to 100. Pass/fail updates the run status.">
        <TextInput
          type="number"
          min={0}
          max={100}
          value={score}
          onChange={(event) => setScore(clampScore(event.target.value))}
        />
      </FormField>
      <FormField label="Evaluator notes">
        <FormTextArea
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          rows={5}
          placeholder="Why this run should pass, fail, or win."
        />
      </FormField>

      <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-1">
        <ToolbarButton
          type="button"
          onClick={() => saveReview(true)}
          disabled={saving}
          variant="success"
        >
          {saving ? "Saving..." : "Pass"}
        </ToolbarButton>
        <ToolbarButton
          type="button"
          onClick={() => saveReview(false)}
          disabled={saving}
          variant="danger"
        >
          {saving ? "Saving..." : "Fail"}
        </ToolbarButton>
        <ToolbarButton
          type="button"
          onClick={() => saveReview()}
          disabled={saving}
        >
          {saving ? "Saving..." : "Save review"}
        </ToolbarButton>
      </div>

      <ToolbarButton
        type="button"
        onClick={selectWinner}
        disabled={busySelect || selected || run.status === "running"}
        variant={selected ? "secondary" : "primary"}
        className="w-full justify-center"
      >
        {selected ? "Winner selected" : busySelect ? "Selecting..." : "Select as winner"}
      </ToolbarButton>

      {err && (
        <InlineAlert tone="red" role="alert" className="text-xs">
          {err}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}
