import { useEffect, useState, type FormEvent } from "react";
import {
  enforceComplianceRetention,
  updateCompliancePolicy,
  type CompliancePolicy,
  type ComplianceFramework,
  type ComplianceRetentionEnforceResult,
} from "../../api";
import {
  Dialog,
  FormField,
  InlineAlert,
  SectionPanel,
  TextArea,
  TextInput,
  ToggleField,
  ToolbarButton,
} from "../DashboardChrome";
import {
  COMPLIANCE_FRAMEWORKS,
  EU_AI_ACT_RETENTION_FLOOR_DAYS,
  formatComplianceDate,
} from "./shared";

type PolicyDraft = {
  frameworks: ComplianceFramework[];
  retentionDays: string;
  legalHold: boolean;
  legalHoldReason: string;
};

function draftFromPolicy(policy: CompliancePolicy): PolicyDraft {
  return {
    frameworks: [...policy.frameworks],
    retentionDays: String(policy.retention_days),
    legalHold: policy.legal_hold,
    legalHoldReason: policy.legal_hold_reason ?? "",
  };
}

/**
 * PolicyPanel — edit the org compliance policy (frameworks, retention window,
 * legal hold) plus the owner-only destructive "enforce retention now" action
 * behind a confirm dialog.
 */
export function PolicyPanel({
  orgSlug,
  policy,
  isOwner,
  onPolicyChanged,
}: {
  orgSlug: string;
  policy: CompliancePolicy;
  isOwner: boolean;
  onPolicyChanged: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<PolicyDraft>(() => draftFromPolicy(policy));
  const [busy, setBusy] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [confirmEnforce, setConfirmEnforce] = useState(false);
  const [enforceErr, setEnforceErr] = useState<string | null>(null);
  const [enforceResult, setEnforceResult] =
    useState<ComplianceRetentionEnforceResult | null>(null);

  // Re-seed the form when a fresh policy arrives (after save or refresh).
  useEffect(() => {
    setDraft(draftFromPolicy(policy));
    setSaveErr(null);
  }, [policy]);

  // Clear transient confirmations when switching organizations.
  useEffect(() => {
    setSaved(false);
    setEnforceErr(null);
    setEnforceResult(null);
  }, [orgSlug]);

  const euAiActActive = draft.frameworks.includes("eu_ai_act");
  const retentionDays = Number.parseInt(draft.retentionDays, 10);
  const retentionValid = Number.isFinite(retentionDays) && retentionDays > 0;
  const retentionBelowFloor =
    euAiActActive && retentionValid && retentionDays < EU_AI_ACT_RETENTION_FLOOR_DAYS;

  function toggleFramework(framework: ComplianceFramework, enabled: boolean) {
    setDraft((current) => ({
      ...current,
      frameworks: enabled
        ? [...current.frameworks, framework]
        : current.frameworks.filter((entry) => entry !== framework),
    }));
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!retentionValid) {
      setSaveErr("Retention days must be a positive number.");
      return;
    }
    setBusy("policy:save");
    setSaveErr(null);
    setSaved(false);
    try {
      await updateCompliancePolicy(orgSlug, {
        frameworks: draft.frameworks,
        retention_days: retentionDays,
        legal_hold: draft.legalHold,
        legal_hold_reason: draft.legalHold
          ? draft.legalHoldReason.trim() || undefined
          : undefined,
      });
      await onPolicyChanged();
      setSaved(true);
    } catch (ex) {
      setSaveErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function handleEnforceRetention() {
    setBusy("retention:enforce");
    setEnforceErr(null);
    setEnforceResult(null);
    try {
      const result = await enforceComplianceRetention(orgSlug);
      setEnforceResult(result);
      setConfirmEnforce(false);
      await onPolicyChanged();
    } catch (ex) {
      setEnforceErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-4">
      <SectionPanel
        title="Compliance policy"
        description={
          policy.persisted
            ? `Last saved ${formatComplianceDate(policy.updated_at)}.`
            : "Running on defaults — save the policy to make it binding for this organization."
        }
      >
        <form onSubmit={handleSave} className="grid gap-4">
          {saveErr && <InlineAlert tone="red">{saveErr}</InlineAlert>}
          {saved && !saveErr && (
            <InlineAlert tone="emerald">Compliance policy saved.</InlineAlert>
          )}

          <fieldset className="grid gap-2">
            <legend className="mb-1 text-xs font-medium text-ink-dim">
              Active frameworks
            </legend>
            {COMPLIANCE_FRAMEWORKS.map((framework) => (
              <ToggleField
                key={framework.id}
                label={framework.label}
                description={framework.description}
                checked={draft.frameworks.includes(framework.id)}
                onCheckedChange={(checked) => toggleFramework(framework.id, checked)}
              />
            ))}
          </fieldset>

          <FormField
            label="Retention days"
            description={
              euAiActActive
                ? `Minimum ${EU_AI_ACT_RETENTION_FLOOR_DAYS} days while the EU AI Act framework is active (policy floor: ${policy.retention_floor_days} days).`
                : `Policy floor: ${policy.retention_floor_days} days.`
            }
            className="max-w-xs"
          >
            <TextInput
              type="number"
              min={1}
              value={draft.retentionDays}
              invalid={retentionBelowFloor || (!retentionValid && draft.retentionDays !== "")}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  retentionDays: event.target.value,
                }))
              }
            />
          </FormField>
          {retentionBelowFloor && (
            <InlineAlert tone="amber">
              The EU AI Act framework requires at least {EU_AI_ACT_RETENTION_FLOOR_DAYS}{" "}
              retention days — saving below that will be rejected.
            </InlineAlert>
          )}

          <div className="grid gap-2">
            <ToggleField
              label="Legal hold"
              description="Suspends retention deletion until the hold is lifted. Records are kept regardless of the retention window."
              checked={draft.legalHold}
              onCheckedChange={(checked) =>
                setDraft((current) => ({ ...current, legalHold: checked }))
              }
            />
            {draft.legalHold && (
              <FormField
                label="Legal hold reason"
                description="Why records are being held — shown in the audit trail."
              >
                <TextArea
                  rows={2}
                  value={draft.legalHoldReason}
                  placeholder="e.g. Litigation hold for case #1234"
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      legalHoldReason: event.target.value,
                    }))
                  }
                />
              </FormField>
            )}
          </div>

          <div>
            <ToolbarButton
              type="submit"
              variant="primary"
              disabled={busy !== null || !retentionValid}
            >
              {busy === "policy:save" ? "Saving..." : "Save policy"}
            </ToolbarButton>
          </div>
        </form>
      </SectionPanel>

      <SectionPanel
        title="Retention enforcement"
        description="Permanently delete decision records older than the retention window. Owner-only and irreversible."
      >
        <div className="grid gap-3">
          {enforceErr && <InlineAlert tone="red">{enforceErr}</InlineAlert>}
          {enforceResult && (
            <InlineAlert tone="emerald">
              Retention enforced (cutoff {formatComplianceDate(enforceResult.cutoff)}):{" "}
              {enforceResult.receipts_deleted.toLocaleString()} receipts and{" "}
              {enforceResult.admin_actions_deleted.toLocaleString()} admin actions deleted.{" "}
              {enforceResult.note}
            </InlineAlert>
          )}
          {policy.legal_hold && (
            <InlineAlert tone="amber">
              Legal hold is active — lift it before enforcing retention.
            </InlineAlert>
          )}
          {isOwner ? (
            <div>
              <ToolbarButton
                variant="danger"
                disabled={busy !== null}
                onClick={() => setConfirmEnforce(true)}
              >
                Enforce retention now
              </ToolbarButton>
            </div>
          ) : (
            <p className="text-xs text-ink-muted">
              Only organization owners can enforce retention.
            </p>
          )}
        </div>
      </SectionPanel>

      <Dialog
        open={confirmEnforce}
        onClose={busy ? undefined : () => setConfirmEnforce(false)}
        title="Enforce retention now?"
        description={`Decision records older than the ${policy.retention_days.toLocaleString()}-day retention window will be permanently deleted. This cannot be undone.`}
        size="sm"
        actions={
          <>
            <ToolbarButton
              onClick={() => setConfirmEnforce(false)}
              disabled={busy !== null}
            >
              Cancel
            </ToolbarButton>
            <ToolbarButton
              variant="danger"
              onClick={handleEnforceRetention}
              disabled={busy !== null}
            >
              {busy === "retention:enforce" ? "Deleting..." : "Permanently delete"}
            </ToolbarButton>
          </>
        }
      >
        <p className="text-sm leading-relaxed text-ink-soft">
          Evidence exports created after enforcement will no longer include the
          deleted records. If the organization is subject to an audit or legal
          hold, cancel and enable the legal hold instead.
        </p>
      </Dialog>
    </div>
  );
}
