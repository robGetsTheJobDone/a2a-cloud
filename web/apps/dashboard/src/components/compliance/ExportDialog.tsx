import { useState } from "react";
import {
  exportComplianceEvidence,
  type ComplianceDecisionRecordKind,
} from "../../api";
import {
  Dialog,
  FormField,
  InlineAlert,
  TextInput,
  ToggleField,
  ToolbarButton,
} from "../DashboardChrome";
import {
  DECISION_RECORD_KINDS,
  dateInputToIso,
  downloadJsonFile,
  evidencePackFilename,
} from "./shared";

/**
 * ExportDialog — build an evidence pack (date range + record kinds +
 * signature verification) and download it as JSON.
 */
export function ExportDialog({
  orgSlug,
  open,
  onClose,
}: {
  orgSlug: string;
  open: boolean;
  onClose: () => void;
}) {
  const [fromDay, setFromDay] = useState("");
  const [toDay, setToDay] = useState("");
  const [kinds, setKinds] = useState<ComplianceDecisionRecordKind[]>(
    DECISION_RECORD_KINDS.map((kind) => kind.id),
  );
  const [verifySignatures, setVerifySignatures] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function toggleKind(kind: ComplianceDecisionRecordKind, enabled: boolean) {
    setKinds((current) =>
      enabled ? [...current, kind] : current.filter((entry) => entry !== kind),
    );
  }

  async function handleExport() {
    setBusy(true);
    setErr(null);
    try {
      const pack = await exportComplianceEvidence(orgSlug, {
        from_at: dateInputToIso(fromDay),
        to_at: dateInputToIso(toDay, true),
        kinds: kinds.length === DECISION_RECORD_KINDS.length ? undefined : kinds,
        verify_signatures: verifySignatures,
      });
      downloadJsonFile(evidencePackFilename(orgSlug), pack);
      onClose();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={busy ? undefined : onClose}
      title="Export evidence pack"
      description="Download a JSON evidence pack of decision records for auditors and regulators."
      size="md"
      actions={
        <>
          <ToolbarButton onClick={onClose} disabled={busy}>
            Cancel
          </ToolbarButton>
          <ToolbarButton
            variant="primary"
            onClick={handleExport}
            disabled={busy || kinds.length === 0}
          >
            {busy ? "Building pack..." : "Export JSON"}
          </ToolbarButton>
        </>
      }
    >
      <div className="grid gap-4">
        {err && <InlineAlert tone="red">{err}</InlineAlert>}

        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="From" description="Leave empty for the oldest record.">
            <TextInput
              type="date"
              value={fromDay}
              onChange={(event) => setFromDay(event.target.value)}
            />
          </FormField>
          <FormField label="To" description="Leave empty for today.">
            <TextInput
              type="date"
              value={toDay}
              onChange={(event) => setToDay(event.target.value)}
            />
          </FormField>
        </div>

        <fieldset className="grid gap-2">
          <legend className="mb-1 text-xs font-medium text-ink-dim">
            Record kinds
          </legend>
          {DECISION_RECORD_KINDS.map((kind) => (
            <ToggleField
              key={kind.id}
              label={kind.label}
              checked={kinds.includes(kind.id)}
              onCheckedChange={(checked) => toggleKind(kind.id, checked)}
            />
          ))}
          {kinds.length === 0 && (
            <InlineAlert tone="amber">Select at least one record kind.</InlineAlert>
          )}
        </fieldset>

        <ToggleField
          label="Verify signatures"
          description="Re-verify every signed record while building the pack and include the verification result."
          checked={verifySignatures}
          onCheckedChange={setVerifySignatures}
        />
      </div>
    </Dialog>
  );
}
